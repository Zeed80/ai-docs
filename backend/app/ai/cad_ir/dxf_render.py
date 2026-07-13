"""CAD IR → DXF (master CAD export, editable layers/linetypes).

Coordinate mapping: IR pixel space is y-down; DXF drawing units are mm and
y-up. ``x_mm = x_px * scale``, ``y_mm = (image_height - y_px) * scale``.
Without a known scale the export uses 1 unit = 1 px (conditional units) —
validators flag SCALE_UNKNOWN so the user knows dimensions are not metric.

Arc angles: IR stores cv2 image-space degrees (y-down). Mirroring y negates
angles, and DXF arcs are always counter-clockwise, so an IR arc [a0, a1]
becomes a DXF arc [-max, -min].
"""

from __future__ import annotations

import re

from app.ai.cad_ir.dim_render import arrow_len_mm, dimension_label
from app.ai.cad_ir.annotations import annotation_text
from app.ai.cad_ir.schema import (
    LINE_CLASS_LAYERS,
    TEXT_LAYER,
    AnnotationEntity,
    Arc,
    CadIR,
    Circle,
    DimensionEntity,
    HatchRegion,
    Polyline,
    Segment,
    TextEntity,
)

# ЕСКД two-weight system (ГОСТ 2.303): основная ~0.5 mm, тонкая ~0.25 mm.
_LINEWEIGHT = {"main": 50, "thin": 25}

_LAYER_DEFS = (
    ("OBJECT", 7, "CONTINUOUS"),
    ("OBJECT_THIN", 7, "CONTINUOUS"),
    ("CENTER", 3, "CENTER"),
    ("HIDDEN", 8, "DASHED"),
    ("DIM", 2, "CONTINUOUS"),
    ("HATCH", 5, "CONTINUOUS"),
    (TEXT_LAYER, 7, "CONTINUOUS"),
)


def render_ir_to_dxf(ir: CadIR) -> bytes:
    import io

    import ezdxf
    from ezdxf import units

    doc = ezdxf.new("R2010", setup=True)
    doc.units = units.MM
    doc.header["$INSUNITS"] = units.MM
    doc.header["$MEASUREMENT"] = 1
    # C5: pin the two document GUIDs so the DXF is byte-reproducible from the
    # same IR (ezdxf randomizes them per write otherwise). The version marker
    # timestamp is normalized after write in _normalize_dxf.
    doc.header["$FINGERPRINTGUID"] = "{00000000-0000-0000-0000-000000000000}"
    doc.header["$VERSIONGUID"] = "{00000000-0000-0000-0000-000000000001}"
    for name, color, linetype in _LAYER_DEFS:
        if name not in doc.layers:
            doc.layers.add(name, color=color, linetype=linetype)
    msp = doc.modelspace()

    scale = ir.scale or 1.0
    h = ir.source.image_height

    def pt(x: float, y: float) -> tuple[float, float]:
        return (x * scale, (h - y) * scale)

    for entity in ir.entities:
        layer = LINE_CLASS_LAYERS.get(entity.line_class, "OBJECT")
        attribs = {"layer": layer, "lineweight": _LINEWEIGHT[entity.width_class]}
        if isinstance(entity, Segment):
            msp.add_line(pt(entity.p1.x, entity.p1.y), pt(entity.p2.x, entity.p2.y), dxfattribs=attribs)
        elif isinstance(entity, Circle):
            msp.add_circle(pt(entity.center.x, entity.center.y), entity.radius * scale, dxfattribs=attribs)
        elif isinstance(entity, Arc):
            a0, a1 = sorted((entity.start_angle, entity.end_angle))
            msp.add_arc(
                pt(entity.center.x, entity.center.y),
                entity.radius * scale,
                start_angle=-a1,
                end_angle=-a0,
                dxfattribs=attribs,
            )
        elif isinstance(entity, Polyline):
            msp.add_lwpolyline(
                [pt(p.x, p.y) for p in entity.points],
                close=entity.closed,
                dxfattribs=attribs,
            )
        elif isinstance(entity, TextEntity):
            msp.add_text(
                entity.text,
                dxfattribs={
                    "layer": TEXT_LAYER,
                    "height": max(entity.height * scale, 0.1),
                    "rotation": -entity.rotation,
                },
            ).set_placement(pt(entity.position.x, entity.position.y))
        elif isinstance(entity, DimensionEntity):
            # Export real DIMENSION entities so downstream CAD can edit style,
            # measurement points and labels instead of receiving exploded
            # LINE/SOLID/TEXT graphics.
            dim_attribs = {"layer": "DIM"}
            p1_mm = pt(entity.p1.x, entity.p1.y)
            p2_mm = pt(entity.p2.x, entity.p2.y)
            label_text = dimension_label(entity)
            text = label_text or "<>"
            override = {"dimtxt": 3.5, "dimasz": arrow_len_mm()}
            if entity.kind == "diameter":
                dim = msp.add_diameter_dim_2p(
                    p1_mm, p2_mm, text=text, override=override, dxfattribs=dim_attribs,
                )
            elif entity.kind == "radial":
                dim = msp.add_radius_dim_2p(
                    p1_mm, p2_mm, text=text, override=override, dxfattribs=dim_attribs,
                )
            else:
                dim = msp.add_aligned_dim(
                    p1_mm, p2_mm, distance=0, text=text, override=override, dxfattribs=dim_attribs,
                )
            dim.render()
        elif isinstance(entity, HatchRegion):
            from ezdxf import const

            hatch = msp.add_hatch(dxfattribs={"layer": "HATCH"})
            if entity.pattern == "solid":
                hatch.set_solid_fill(color=7)
            else:
                hatch.set_pattern_fill("ANSI31", scale=max(scale, 0.05) * 10)
            hatch.paths.add_polyline_path(
                [pt(p.x, p.y) for p in entity.boundary], is_closed=True,
                flags=const.BOUNDARY_PATH_EXTERNAL,
            )
            # Nested loops (a section fill with a bolt hole through it) —
            # DEFAULT (not EXTERNAL) flags mark them as inner boundaries the
            # hatch's own fill algorithm subtracts from the outer region.
            for hole in entity.holes:
                hatch.paths.add_polyline_path(
                    [pt(p.x, p.y) for p in hole], is_closed=True,
                    flags=const.BOUNDARY_PATH_DEFAULT,
                )
        elif isinstance(entity, AnnotationEntity):
            # Structured ЕСКД annotations export as text on the DIM layer,
            # with a leader and (for tolerance/datum) a boxed frame — editable
            # CAD geometry, not a flattened glyph.
            text = annotation_text(entity.kind, entity.value, entity.symbol, entity.datum_refs)
            h = max(entity.height * scale, 0.1)
            msp.add_text(
                text, dxfattribs={"layer": "DIM", "height": h},
            ).set_placement(pt(entity.position.x, entity.position.y))
            if entity.leader is not None:
                msp.add_line(
                    pt(entity.position.x, entity.position.y),
                    pt(entity.leader.x, entity.leader.y),
                    dxfattribs={"layer": "DIM"},
                )
            if entity.kind in ("tolerance", "datum"):
                x_mm, y_mm = pt(entity.position.x, entity.position.y)
                w = max(h * len(text) * 0.62, h * 1.6)
                msp.add_lwpolyline(
                    [(x_mm - h * 0.3, y_mm - h * 0.2), (x_mm + w, y_mm - h * 0.2),
                     (x_mm + w, y_mm + h * 1.2), (x_mm - h * 0.3, y_mm + h * 1.2)],
                    close=True, dxfattribs={"layer": "DIM"},
                )

    buf = io.StringIO()
    doc.write(buf)
    return _normalize_dxf(buf.getvalue()).encode("utf-8")


# ezdxf stamps a per-write timestamp and regenerates $VERSIONGUID on every
# write; neutralize both (plus $FINGERPRINTGUID for good measure) so the same
# IR yields byte-identical DXF (C5 reproducibility).
_EZDXF_MARKER = re.compile(r"(\d+\.\d+\.\d+) @ \d{4}-\d{2}-\d{2}T[\d:.+-]+")
_GUID_HEADER = re.compile(
    r"(\$(?:VERSION|FINGERPRINT)GUID\r?\n\s*2\r?\n)\{[0-9A-Fa-f-]+\}"
)


def _normalize_dxf(text: str) -> str:
    text = _EZDXF_MARKER.sub(r"\1 @ 1970-01-01T00:00:00+00:00", text)
    text = _GUID_HEADER.sub(r"\g<1>{00000000-0000-0000-0000-000000000000}", text)
    return text


def render_dxf_to_pdf(dxf_bytes: bytes) -> bytes:
    """Print-ready PDF of the master DXF (I4): the same layers, linetypes and
    lineweights CAD sees, rendered vector-to-vector via ezdxf's matplotlib
    backend. Used for the editor's «Печать / PDF» — a drawing you can hand to
    a shop, not a screenshot."""
    import io

    import ezdxf
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    doc = ezdxf.read(io.StringIO(dxf_bytes.decode("utf-8")))
    msp = doc.modelspace()
    fig = plt.figure()
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    try:
        backend = MatplotlibBackend(ax)
        ctx = RenderContext(doc)
        Frontend(ctx, backend).draw_layout(msp, finalize=True)
        out = io.BytesIO()
        fig.savefig(out, format="pdf", dpi=300)
        return out.getvalue()
    finally:
        plt.close(fig)
