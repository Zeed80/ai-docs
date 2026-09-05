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
    material: str = "",
    scale: str = "",
    mass: str = "",
    sheet: str = "",
    sheets: str = "",
) -> list[Entity]:
    """Sheet border + corner stamp, in IR pixel space (y-down, matching the
    blank sheet's own convention — no flipping needed against техdraw's SVG,
    which is also top-left-origin/y-down)."""

    def px(x_mm: float, y_mm: float) -> Point:
        return Point(x=x_mm * px_per_mm, y=y_mm * px_per_mm)

    main = {
        "line_class": "contour",
        "width_class": "main",
        "origin": "human",
        "assurance": "human_approved",
    }
    thin = {
        "line_class": "contour",
        "width_class": "thin",
        "origin": "human",
        "assurance": "human_approved",
    }

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
    # ГОСТ 2.104 form 1, 185x55. The rulings below are the form's own graphs,
    # not a decorative grid: the left 65 mm carry Изм./Лист/№ докум./Подп./Дата
    # over the approval roles, the middle band carries наименование and
    # материал, and the right 65 mm carry Лит./Масса/Масштаб over Лист/Листов.
    #
    # The previous stamp drew six evenly spaced rulings and wrote the name
    # across one of them, so on a real sheet the part's name sat on a line and
    # material, scale and mass had nowhere to go at all — they were read off
    # the drawing and then dropped.
    for yy in (5, 10, 15, 20, 25, 30, 35, 40, 45, 50):
        entities.append(Segment(p1=px(x0, y0 + yy), p2=px(x0 + TB_W_MM, y0 + yy), **thin))
    # The Изм. column is one cell wide in the header row only: carrying it down
    # through the role rows puts a ruling straight through "Разраб." and
    # "Т.контр.", which are written across the first two columns.
    entities.append(Segment(p1=px(x0 + 7, y0), p2=px(x0 + 7, y0 + 5), **thin))
    for xx in (17, 40, 55, 65, 120, 137, 152, 167):
        entities.append(Segment(p1=px(x0 + xx, y0), p2=px(x0 + xx, y0 + 25), **thin))
    for xx in (65, 120, 137, 152, 167):
        entities.append(Segment(p1=px(x0 + xx, y0 + 25), p2=px(x0 + xx, y0 + TB_H_MM), **thin))

    def label(
        text: str,
        x_mm: float,
        y_mm: float,
        height_mm: float = 3.0,
        fit_mm: float | None = None,
    ) -> None:
        """Write into a stamp cell, shrinking to stay inside it.

        A ГОСТ graph is a fixed width and the value is whatever the sheet
        said: "Шпиндель обрабатывающего центра V-10" at nominal height ran
        clean through Лит., Масса and Масштаб and overwrote all three. The
        text is scaled down to fit instead — unreadably small is still a
        smaller lie than a value written over three other values.
        """
        if not text:
            return
        if fit_mm:
            # ~0.6 of the text height per character at this face.
            needed = len(text) * height_mm * 0.6
            if needed > fit_mm:
                height_mm = max(1.8, height_mm * fit_mm / needed)
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

    # Column headers of the revision/approval block, so a printed sheet is a
    # ГОСТ stamp and not an anonymous grid.
    for text, xx in (("Изм.", 1), ("Лист", 8), ("№ докум.", 19), ("Подп.", 42), ("Дата", 57)):
        label(text, x0 + xx, y0 + 4, 2.2)
    for text, yy in (("Разраб.", 9), ("Пров.", 14), ("Т.контр.", 19), ("Н.контр.", 24)):
        label(text, x0 + 1, y0 + yy, 2.2)

    # Right block headers and their values.
    label("Лит.", x0 + 121, y0 + 29, 2.2)
    label("Масса", x0 + 138, y0 + 29, 2.2)
    label("Масштаб", x0 + 153, y0 + 29, 2.2)
    label(mass, x0 + 138, y0 + 38, 3.5)
    label(scale, x0 + 154, y0 + 38, 3.5)
    label("Лист", x0 + 121, y0 + 47, 2.2)
    label(sheet, x0 + 131, y0 + 47, 3.0)
    label("Листов", x0 + 138, y0 + 47, 2.2)
    label(sheets, x0 + 152, y0 + 47, 3.0)

    # Наименование and материал: the two graphs a reader looks at first.
    label(designation, x0 + 68, y0 + 29, 4.0, fit_mm=50.0)
    label(name, x0 + 68, y0 + 38, 4.2, fit_mm=50.0)
    label(material, x0 + 68, y0 + 48, 3.2, fit_mm=50.0)
    label(company, x0 + 121, y0 + 54, 2.6)

    return entities
