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

from app.ai.cad_ir.dim_render import arrow_len_mm, dimension_label
from app.ai.cad_ir.schema import (
    LINE_CLASS_LAYERS,
    TEXT_LAYER,
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

    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")
