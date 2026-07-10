"""ShaftSpec → CAD IR (Ф6.1): the first slice of unifying techdraw's
rendering onto the same IR the vectorize pipeline already renders through
(``svg_render``/``dxf_render``/``png_render``), instead of techdraw's own
duplicated bespoke SVG-drawing (``_render_shaft``) and DXF-drawing
(``_dxf_draw_shaft``) code paths, which had already drifted apart (the DXF
path is missing chamfers/section-hatch/roughness that the SVG path draws).

Deliberately scoped to ``ShaftSpec`` front-view geometry only — PlateSpec,
AssemblySpec, and isometric views stay on the legacy renderer for now. This
module is NOT wired into the production ``/technology`` techdraw endpoints
yet; it is a proven, tested bridge, not a cutover.
"""

from __future__ import annotations

from app.ai import techdraw_reference as tdref
from app.ai.cad_ir.schema import (
    CadIR,
    DimensionEntity,
    Entity,
    Point,
    Segment,
    SheetInfo,
    SourceInfo,
)

_MARGIN_MM = 20.0
_DIM_ROW_GAP_MM = 14.0
_TOTAL_DIM_GAP_MM = 10.0


def _spec_dia_label(diameter: float, tolerance: str) -> str:
    return f"{diameter:g}{tolerance}"


def shaft_spec_to_ir(spec, px_per_mm: float = 4.0) -> CadIR:
    """Front-view stepped-shaft profile as CAD IR entities. Geometry mirrors
    ``techdraw._dxf_draw_shaft`` exactly (same per-segment top/bottom lines,
    step transitions, bore/thread centerlines, dimension placement) so this
    is a genuine like-for-like port, not a reinterpretation."""
    from app.ai.techdraw import ShaftSpec  # local import: avoid a cycle (techdraw imports nothing from here)

    s: ShaftSpec = spec if isinstance(spec, ShaftSpec) else ShaftSpec(**spec)
    segments = s.segments
    max_d = max(seg.diameter for seg in segments)
    total_len = sum(seg.length for seg in segments)
    dim_base_offset = max_d / 2 + _DIM_ROW_GAP_MM

    cy_mm = dim_base_offset + _TOTAL_DIM_GAP_MM + _MARGIN_MM
    width_mm = total_len + 2 * _MARGIN_MM
    height_mm = cy_mm + max_d / 2 + _MARGIN_MM
    image_width_px = width_mm * px_per_mm
    image_height_px = height_mm * px_per_mm

    def P(x_mm: float, y_mm: float) -> Point:
        # y_mm is in the shaft's own local frame (0 = centerline, + = up).
        # This is the exact inverse of dxf_render.pt()'s px->mm/y-flip, so
        # feeding legacy-identical mm coordinates through here and back out
        # through dxf_render reproduces those same mm coordinates verbatim.
        return Point(
            x=(x_mm + _MARGIN_MM) * px_per_mm,
            y=image_height_px - (y_mm + cy_mm) * px_per_mm,
        )

    common = {"line_class": "contour", "width_class": "main", "origin": "spec", "assurance": "constraint_validated"}
    axis = {"line_class": "axis", "width_class": "thin", "origin": "spec", "assurance": "constraint_validated"}
    dim_common = {"origin": "spec", "assurance": "constraint_validated"}

    entities: list[Entity] = []
    x = 0.0
    prev_h: float | None = None
    for seg in segments:
        w, h = seg.length, seg.diameter
        top, bot = h / 2, -h / 2
        entities.append(Segment(p1=P(x, top), p2=P(x + w, top), **common))
        entities.append(Segment(p1=P(x, bot), p2=P(x + w, bot), **common))
        if prev_h is None or abs(prev_h - h) > 1e-9:
            yy = max((prev_h or h) / 2, h / 2)
            entities.append(Segment(p1=P(x, -yy), p2=P(x, yy), **common))
        if seg.bore_diameter:
            bh = seg.bore_diameter
            entities.append(Segment(p1=P(x, bh / 2), p2=P(x + w, bh / 2), **axis))
            entities.append(Segment(p1=P(x, -bh / 2), p2=P(x + w, -bh / 2), **axis))
        thread_spec = tdref.parse_thread(seg.thread) if seg.thread else None
        if thread_spec:
            minor = tdref.minor_diameter_mm(thread_spec)
            entities.append(Segment(p1=P(x, minor / 2), p2=P(x + w, minor / 2), **axis))
            entities.append(Segment(p1=P(x, -minor / 2), p2=P(x + w, -minor / 2), **axis))

        entities.append(
            DimensionEntity(
                p1=P(x, -dim_base_offset), p2=P(x + w, -dim_base_offset),
                kind="linear", text=f"{seg.length:g}", value_mm=seg.length, **dim_common,
            )
        )
        dia_text = seg.thread or _spec_dia_label(seg.diameter, seg.tolerance)
        entities.append(
            DimensionEntity(
                p1=P(x + w / 2, bot), p2=P(x + w / 2, top),
                kind="diameter", text=dia_text, value_mm=seg.diameter,
                tolerance=seg.tolerance or None, **dim_common,
            )
        )
        prev_h = h
        x += w

    entities.append(Segment(p1=P(x, -prev_h / 2), p2=P(x, prev_h / 2), **common))
    entities.append(Segment(p1=P(-6, 0), p2=P(total_len + 6, 0), **axis))
    entities.append(
        DimensionEntity(
            p1=P(0, -dim_base_offset - _TOTAL_DIM_GAP_MM),
            p2=P(total_len, -dim_base_offset - _TOTAL_DIM_GAP_MM),
            kind="linear", text=f"{total_len:g}", value_mm=total_len, **dim_common,
        )
    )

    return CadIR(
        source=SourceInfo(image_width=int(image_width_px), image_height=int(image_height_px), kind="blank"),
        scale=1.0 / px_per_mm,
        sheet=SheetInfo(),
        entities=entities,
        recognizer_used="spec",
    )
