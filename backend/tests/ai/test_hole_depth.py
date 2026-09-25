"""Глубина отверстия пластины по виду на толщину (X1, Ф2 `line_style`)."""

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers import hole_depth
from app.ai.cad_recognize.verifiers.housing_views import HousingViews
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Лист 1 мм = 10 px. План 100 × 60 мм: x 100…1100, y 100…700 (низ — 700).
# Вид на толщину 20 мм справа: x 1300…1500, строки общие с планом.
_PLAN = ViewFrame(bbox_px=(100, 100, 1100, 700), mm_per_px=0.1, origin_px=(100.0, 700.0))
_SIDE = (1300.0, 100.0, 1500.0, 700.0)


def _dashed(draw, y: float, x_from: float, x_to: float) -> None:
    """Невидимый контур: штрих 50 px, пропуск 15 px (5 и 1,5 мм)."""
    step = 1 if x_to > x_from else -1
    x = x_from
    while (x - x_to) * step < 0:
        end = x + step * 50
        if (end - x_to) * step > 0:
            end = x_to
        draw.line([(x, y), (end, y)], fill=0, width=3)
        x = end + step * 15


def _sheet() -> np.ndarray:
    image = Image.new("L", (1700, 800), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle(_PLAN.bbox_px, outline=0, width=6)
    draw.rectangle(_SIDE, outline=0, width=6)
    # Отверстие 0: сквозное Ø10 на y = 45 мм (строки 200 и 300).
    for row in (200, 300):
        _dashed(draw, row, 1500, 1300)
    # Отверстие 1: глухое Ø10 на 8 мм от правой грани (y = 15 мм, строки 500/600).
    for row in (500, 600):
        _dashed(draw, row, 1500, 1420)
    draw.line([(1420, 500), (1420, 600)], fill=0, width=3)
    # Отверстие 2: сквозное Ø14 на y = 20 мм (строки 430…570) — через его
    # полосу проходит дно отверстия 1, которое начинается НЕ на его строках.
    for row in (430, 570):
        _dashed(draw, row, 1500, 1300)
    return np.asarray(image)


def _profile(**overrides) -> dict:
    holes = [
        {"center_x_mm": -20.0, "center_y_mm": 15.0, "diameter_mm": 10.0},
        {"center_x_mm": 20.0, "center_y_mm": -15.0, "diameter_mm": 10.0, "depth_mm": 8.0},
        {"center_x_mm": 0.0, "center_y_mm": -10.0, "diameter_mm": 14.0},
    ]
    return {"width_mm": 100.0, "height_mm": 60.0, "thickness_mm": 20.0, "holes": holes, **overrides}


def _patch(monkeypatch):
    monkeypatch.setattr(
        "app.ai.cad_recognize.verifiers.housing_views.locate_housing_views",
        lambda *_a, **_k: HousingViews(_PLAN, None, _SIDE, 20.0, "тест"),
    )


def test_through_and_blind_holes_are_told_apart_by_the_bottom_line(monkeypatch):
    _patch(monkeypatch)

    items = hole_depth.verify_hole_depths(_sheet(), _profile())

    assert [item["status"] for item in items] == ["confirmed", "confirmed", "confirmed"]
    assert items[0]["measured"] == {"through": True}
    assert items[1]["measured"]["through"] is False
    assert abs(items[1]["measured"]["depth_mm"] - 8.0) <= 0.5
    # Дно соседнего отверстия через полосу сквозного — не его дно.
    assert items[2]["measured"] == {"through": True}


def test_a_blind_hole_read_as_through_is_refuted(monkeypatch):
    """Ридер «гл.» не читает: глухое отверстие приходило сквозным молча."""
    _patch(monkeypatch)
    profile = _profile()
    profile["holes"][1].pop("depth_mm")
    profile["holes"][0]["depth_mm"] = 10.0

    items = hole_depth.verify_hole_depths(_sheet(), profile)

    assert items[0]["status"] == "refuted" and items[0]["measured"] == {"through": True}
    assert items[1]["status"] == "refuted" and items[1]["measured"]["through"] is False


def test_a_blurred_sheet_is_not_measured(monkeypatch):
    from PIL import ImageFilter

    _patch(monkeypatch)
    blurred = np.asarray(Image.fromarray(_sheet()).filter(ImageFilter.GaussianBlur(6)))

    items = hole_depth.verify_hole_depths(blurred, _profile())

    assert {item["status"] for item in items} == {"unmeasurable"}


def test_a_depth_is_adopted_only_when_the_label_matches_the_measurement():
    """Надпись у отверстия по вырезу: «Ø11 гл.15» при замере 14,8 — принято;
    «гл.8» и надпись без глубины — нет."""
    import asyncio
    import io

    from app.ai.cad_recognize.verifiers.reask import reask_hole_depths
    from app.ai.cad_recognize.verifiers.reconcile import apply_hole_depths

    buffer = io.BytesIO()
    Image.new("RGB", (400, 400), "white").save(buffer, format="PNG")
    spec = {"main_view": {"profile": {"holes": [{"diameter_mm": 11.0}]}}}
    report = {
        "items": [
            {
                "kind": "plate_hole",
                "path": "main_view.profile.holes[0]",
                "evidence_bbox_px": [180, 180, 220, 220],
            },
            {
                "kind": "hole_depth",
                "path": "main_view.profile.holes[0]",
                "status": "refuted",
                "read": {"through": True},
                "measured": {"through": False, "depth_mm": 14.8},
            },
        ]
    }

    async def says(value):
        async def ask(_prompt, _crop):
            return {"label": None if value is None else f"Ø11 гл.{value}"}

        return await reask_hole_depths(buffer.getvalue(), spec, report, ask=ask)

    adopted = asyncio.run(says(15))
    assert [d["value"] for d in adopted] == [15.0]
    assert apply_hole_depths(spec, adopted)["main_view"]["profile"]["holes"][0]["depth_mm"] == 15.0
    assert asyncio.run(says(8)) == []
    assert asyncio.run(says(None)) == []
