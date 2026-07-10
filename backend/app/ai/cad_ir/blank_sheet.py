"""ГОСТ 2.301 sheet frame + ГОСТ 2.104 form-1 corner stamp as real CAD IR
entities, for the blank-sheet manual-drafting entry point (Ф5.5).

Geometry constants mirror ``techdraw.py``'s SVG renderer intentionally (same
standard, same numbers — ``_new_sheet``/``_title_block``) but here they land
as editable Segment/TextEntity entities in the IR instead of static SVG
strokes: the user can click and edit the stamp text (designation, name,
company) exactly like any other text entity, through the same PATCH /ir
the rest of the editor already uses.
"""

from __future__ import annotations

from app.ai import techdraw_reference as tdref
from app.ai.cad_ir.schema import Entity, Point, Segment, TextEntity

_FRAME_LEFT_MARGIN_MM = 20.0
_FRAME_MARGIN_MM = 5.0
TB_W_MM = tdref.TITLE_BLOCK_W_MM
TB_H_MM = tdref.TITLE_BLOCK_H_MM


def frame_and_title_block_entities(
    width_mm: float,
    height_mm: float,
    px_per_mm: float,
    *,
    name: str = "",
    designation: str = "",
    company: str = "",
) -> list[Entity]:
    """Sheet border + corner stamp, in IR pixel space (y-down, matching the
    blank sheet's own convention — no flipping needed against техdraw's SVG,
    which is also top-left-origin/y-down)."""

    def px(x_mm: float, y_mm: float) -> Point:
        return Point(x=x_mm * px_per_mm, y=y_mm * px_per_mm)

    main = {"line_class": "contour", "width_class": "main", "origin": "human", "assurance": "human_approved"}
    thin = {"line_class": "contour", "width_class": "thin", "origin": "human", "assurance": "human_approved"}

    fx0, fy0 = _FRAME_LEFT_MARGIN_MM, _FRAME_MARGIN_MM
    fx1, fy1 = width_mm - _FRAME_MARGIN_MM, height_mm - _FRAME_MARGIN_MM
    entities: list[Entity] = [
        Segment(p1=px(fx0, fy0), p2=px(fx1, fy0), **main),
        Segment(p1=px(fx1, fy0), p2=px(fx1, fy1), **main),
        Segment(p1=px(fx1, fy1), p2=px(fx0, fy1), **main),
        Segment(p1=px(fx0, fy1), p2=px(fx0, fy0), **main),
    ]

    x0 = width_mm - 25.0 - TB_W_MM
    y0 = height_mm - 10.0 - TB_H_MM
    entities += [
        Segment(p1=px(x0, y0), p2=px(x0 + TB_W_MM, y0), **main),
        Segment(p1=px(x0 + TB_W_MM, y0), p2=px(x0 + TB_W_MM, y0 + TB_H_MM), **main),
        Segment(p1=px(x0 + TB_W_MM, y0 + TB_H_MM), p2=px(x0, y0 + TB_H_MM), **main),
        Segment(p1=px(x0, y0 + TB_H_MM), p2=px(x0, y0), **main),
    ]
    for yy in (8, 16, 24, 32, 40, 47):
        entities.append(Segment(p1=px(x0, y0 + yy), p2=px(x0 + TB_W_MM, y0 + yy), **thin))
    entities.append(Segment(p1=px(x0 + 65, y0), p2=px(x0 + 65, y0 + 47), **thin))
    entities.append(Segment(p1=px(x0 + 135, y0), p2=px(x0 + 135, y0 + TB_H_MM), **thin))

    def label(text: str, x_mm: float, y_mm: float, height_mm: float = 3.0) -> None:
        if not text:
            return
        entities.append(
            TextEntity(
                position=px(x_mm, y_mm),
                text=text,
                height=height_mm * px_per_mm,
                line_class="dim",
                width_class="thin",
                origin="human",
                assurance="human_approved",
            )
        )

    label(name, x0 + 70, y0 + 16, 4.2)
    label(designation, x0 + 70, y0 + 32, 4.0)
    label(company, x0 + 70, y0 + 55, 2.6)

    return entities
