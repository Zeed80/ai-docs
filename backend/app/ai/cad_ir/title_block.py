"""Structured основная надпись (ГОСТ 2.104 form 1) editor over the CAD IR (C3).

The blank-sheet entry point already draws the frame + stamp as raw editable
entities, but the stamp *fields* (обозначение, наименование, материал,
масштаб, масса, подписи) were unstructured free text a user had to click and
edit cell by cell. This module makes them a structured record on
``ir.sheet.title_block["fields"]`` and (re)renders them into the correct
ГОСТ 2.104 cells with a single ``set_title_block`` operation.

Cell placement is PROPORTIONAL to the stamp region (the fixed 185×55 mm form
mapped onto whatever pixel box the region occupies), so it works the same on
a blank sheet (region computed from the sheet size) and on a scanned sheet
whose stamp was detected at its own coordinates — no dependency on a known
mm/px scale. Generated entities are tagged ``evidence=["title_block_text"]``
(labels) / ``["title_block_frame"]`` (lines) so a field edit replaces only
the labels and never disturbs the drawing or the frame.
"""

from __future__ import annotations

from typing import Any

from app.ai import techdraw_reference as tdref
from app.ai.cad_ir.schema import CadIR, Entity, Point, Segment, TextEntity

TB_W_MM = tdref.TITLE_BLOCK_W_MM  # 185
TB_H_MM = tdref.TITLE_BLOCK_H_MM  # 55

# The editable fields of ГОСТ 2.104 form 1. Order = form order for the UI.
TITLE_BLOCK_FIELDS: tuple[str, ...] = (
    "designation",   # обозначение (децимальный номер)
    "name",          # наименование
    "material",      # материал
    "scale",         # масштаб, напр. "1:2"
    "mass_kg",       # масса, кг
    "litera",        # литера (У/О1/…)
    "sheet_no",      # лист
    "sheet_count",   # листов
    "developer",     # разраб.
    "checked_by",    # пров.
    "norm_checked_by",  # н.контр.
    "approved_by",   # утв.
    "date",          # дата
    "company",       # предприятие
)

_TEXT_TAG = "title_block_text"
_FRAME_TAG = "title_block_frame"

_FRAME_LEFT_MARGIN_MM = 20.0
_FRAME_MARGIN_MM = 5.0
_STAMP_RIGHT_MM = 25.0   # stamp inset from the sheet's right edge
_STAMP_BOTTOM_MM = 10.0  # stamp inset from the sheet's bottom edge


def _get_fields(ir: CadIR) -> dict[str, Any]:
    tb = ir.sheet.title_block or {}
    fields = tb.get("fields")
    return dict(fields) if isinstance(fields, dict) else {}


def stamp_region_px(ir: CadIR) -> tuple[float, float, float, float] | None:
    """The stamp bounding box in source pixels: the detected/stored region if
    present, else the standard bottom-right position computed from the sheet's
    known size and scale. None when neither is available."""
    tb = ir.sheet.title_block or {}
    region = tb.get("region")
    if isinstance(region, dict) and all(k in region for k in ("x0", "y0", "x1", "y1")):
        return (float(region["x0"]), float(region["y0"]),
                float(region["x1"]), float(region["y1"]))
    if ir.scale and ir.sheet.width_mm and ir.sheet.height_mm:
        ppm = 1.0 / ir.scale
        x0 = (ir.sheet.width_mm - _STAMP_RIGHT_MM - TB_W_MM) * ppm
        y0 = (ir.sheet.height_mm - _STAMP_BOTTOM_MM - TB_H_MM) * ppm
        return (x0, y0, x0 + TB_W_MM * ppm, y0 + TB_H_MM * ppm)
    return None


def _frame_and_grid(ir: CadIR) -> list[Entity]:
    """Sheet border + ГОСТ 2.104 stamp grid, in px, tagged for replacement.
    Only meaningful with a known scale and sheet size."""
    if not (ir.scale and ir.sheet.width_mm and ir.sheet.height_mm):
        return []
    ppm = 1.0 / ir.scale
    w, h = ir.sheet.width_mm, ir.sheet.height_mm

    def px(x_mm: float, y_mm: float) -> Point:
        return Point(x=x_mm * ppm, y=y_mm * ppm)

    main = {"line_class": "contour", "width_class": "main", "origin": "human",
            "assurance": "human_approved", "evidence": [_FRAME_TAG]}
    thin = {"line_class": "contour", "width_class": "thin", "origin": "human",
            "assurance": "human_approved", "evidence": [_FRAME_TAG]}
    fx0, fy0 = _FRAME_LEFT_MARGIN_MM, _FRAME_MARGIN_MM
    fx1, fy1 = w - _FRAME_MARGIN_MM, h - _FRAME_MARGIN_MM
    x0 = w - _STAMP_RIGHT_MM - TB_W_MM
    y0 = h - _STAMP_BOTTOM_MM - TB_H_MM
    ents: list[Entity] = [
        Segment(p1=px(fx0, fy0), p2=px(fx1, fy0), **main),
        Segment(p1=px(fx1, fy0), p2=px(fx1, fy1), **main),
        Segment(p1=px(fx1, fy1), p2=px(fx0, fy1), **main),
        Segment(p1=px(fx0, fy1), p2=px(fx0, fy0), **main),
        Segment(p1=px(x0, y0), p2=px(x0 + TB_W_MM, y0), **main),
        Segment(p1=px(x0 + TB_W_MM, y0), p2=px(x0 + TB_W_MM, y0 + TB_H_MM), **main),
        Segment(p1=px(x0 + TB_W_MM, y0 + TB_H_MM), p2=px(x0, y0 + TB_H_MM), **main),
        Segment(p1=px(x0, y0 + TB_H_MM), p2=px(x0, y0), **main),
    ]
    for yy in (8, 16, 24, 32, 40, 47):
        ents.append(Segment(p1=px(x0, y0 + yy), p2=px(x0 + TB_W_MM, y0 + yy), **thin))
    ents.append(Segment(p1=px(x0 + 65, y0), p2=px(x0 + 65, y0 + 47), **thin))
    ents.append(Segment(p1=px(x0 + 135, y0), p2=px(x0 + 135, y0 + TB_H_MM), **thin))
    return ents


def _render_labels(region: tuple[float, float, float, float], fields: dict[str, Any]) -> list[Entity]:
    """Field text placed into the ГОСТ 2.104 cells, proportional to ``region``
    (the 185×55 form mapped onto the region's pixel box)."""
    x0, y0, x1, y1 = region
    w, h = x1 - x0, y1 - y0

    def at(mm_x: float, mm_y: float) -> Point:
        return Point(x=x0 + (mm_x / TB_W_MM) * w, y=y0 + (mm_y / TB_H_MM) * h)

    def height_px(mm: float) -> float:
        return max(1.0, (mm / TB_H_MM) * h)

    ents: list[Entity] = []

    def put(text: str, mm_x: float, mm_y: float, size_mm: float) -> None:
        s = (text or "").strip()
        if not s:
            return
        ents.append(TextEntity(
            position=at(mm_x, mm_y),
            text=s,
            height=height_px(size_mm),
            line_class="dim",
            width_class="thin",
            origin="human",
            assurance="human_approved",
            evidence=[_TEXT_TAG],
        ))

    def sval(key: str) -> str:
        v = fields.get(key)
        return "" if v is None else str(v)

    # Central column: name (наименование) + designation (обозначение).
    # Heights are nominal ГОСТ 2.304 sizes so the generated stamp does not
    # trip the very ESKD_TEXT_HEIGHT check this pipeline enforces.
    put(sval("name"), 100, 14, 5.0)
    put(sval("designation"), 100, 30, 5.0)
    # Right column: material / litera / mass / sheet / scale.
    put("Материал", 160, 6, 2.4)
    put(sval("material")[:32], 160, 12, 2.3)
    put("Лит.", 140, 20, 2.0)
    put(sval("litera"), 155, 20, 2.2)
    mass = fields.get("mass_kg")
    put("Масса", 140, 27, 2.0)
    put(("" if mass in (None, "") else f"{float(mass):g}"), 160, 27, 2.2)
    put(f"Лист {sval('sheet_no') or '1'}", 140, 34, 2.0)
    put(f"Листов {sval('sheet_count') or '1'}", 140, 41, 2.0)
    put("Масштаб", 145, 52, 2.4)
    put(sval("scale"), 168, 52, 3.4)
    # Bottom-left signatures.
    for label_text, yy, key in (
        ("Разраб.", 6, "developer"),
        ("Пров.", 14, "checked_by"),
        ("Н.контр.", 22, "norm_checked_by"),
        ("Утв.", 30, "approved_by"),
    ):
        put(label_text, 2, yy, 2.4)
        put(sval(key)[:14], 30, yy, 2.6)
    put(sval("date"), 55, 44, 2.2)
    put(sval("company"), 100, 53, 2.6)
    return ents


def apply_title_block(ir: CadIR, fields: dict[str, Any]) -> int:
    """Store the structured stamp fields and (re)render the label text into the
    ГОСТ 2.104 cells. Creates the frame + stamp grid when the sheet has none
    (and a scale is known). Returns the number of label entities placed.

    Idempotent: prior title-block labels are replaced, never accumulated;
    frame lines are only added when missing.
    """
    merged = {**_get_fields(ir), **{k: v for k, v in fields.items() if k in TITLE_BLOCK_FIELDS}}

    # Drop previously generated labels; keep everything else (incl. the frame).
    ir.entities = [
        e for e in ir.entities
        if _TEXT_TAG not in (e.evidence or [])
    ]

    need_frame = not ir.sheet.frame and not any(
        _FRAME_TAG in (e.evidence or []) for e in ir.entities
    )
    if need_frame:
        frame = _frame_and_grid(ir)
        if frame:
            ir.entities.extend(frame)
            ir.sheet.frame = True

    region = stamp_region_px(ir)
    labels: list[Entity] = _render_labels(region, merged) if region else []
    ir.entities.extend(labels)

    tb = dict(ir.sheet.title_block or {})
    tb["fields"] = merged
    if region and "region" not in tb:
        tb["region"] = {"x0": region[0], "y0": region[1], "x1": region[2], "y1": region[3]}
    if merged.get("scale"):
        tb["scale"] = merged["scale"]
    ir.sheet.title_block = tb
    return len(labels)
