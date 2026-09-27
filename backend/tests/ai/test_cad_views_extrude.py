"""Метод `views`, D2: деталь выдавливанием по контуру вида в плане."""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")


def _plate_sheet() -> tuple[np.ndarray, float]:
    """Г-образная пластина 90 × 100 (10 px/мм), основная 7 px, тонкие 3 px."""
    k = 10.0
    sheet = np.full((1500, 1400), 255, np.uint8)
    ox, oy = 200, 1250  # левый нижний угол детали на листе
    poly = [(0, 0), (90, 0), (90, 100), (70, 100), (70, 30), (0, 30)]
    pts = np.array([[int(ox + x * k), int(oy - y * k)] for x, y in poly], np.int32)
    cv2.polylines(sheet, [pts], True, 0, 7)
    for cx, cy, d in ((16, 14, 16), (80, 14, 10)):
        cv2.circle(sheet, (int(ox + cx * k), int(oy - cy * k)), int(d / 2 * k), 0, 7)
    # Тонкие размерные и выносные линии, касающиеся контура.
    cv2.line(sheet, (ox, oy), (ox, oy + 150), 0, 3)
    cv2.line(sheet, (ox + 900, oy), (ox + 900, oy + 150), 0, 3)
    cv2.line(sheet, (ox, oy + 120), (ox + 900, oy + 120), 0, 3)
    cv2.line(sheet, (ox + 900, oy - 1000), (ox + 1050, oy - 1000), 0, 3)
    cv2.line(sheet, (ox + 1020, oy), (ox + 1020, oy - 1000), 0, 3)
    cv2.line(sheet, (ox + 900, oy), (ox + 1050, oy), 0, 3)
    return sheet, k


def test_l_plate_extruded_from_plan_outline_and_thickness_label():
    from app.ai.cad_views.extrude_body import build_extrude
    from app.ai.cad_views.sheet_reading import Region, SheetReading

    sheet, _k = _plate_sheet()
    reading = SheetReading("detail", 1, [Region(1, (150, 200, 1300, 1450), "view", "главный вид")])
    result = build_extrude(sheet, reading, ["90", "100", "30", "Ø16", "Ø10", "s3", "64", "14"])
    assert result.ok, result.reason
    assert result.profile["thickness_mm"] == 3.0
    xs = [p["to"][0] for p in result.profile["sketch"]]
    ys = [p["to"][1] for p in result.profile["sketch"]]
    assert max(xs) - min(xs) == pytest.approx(90, rel=0.02)
    assert max(ys) - min(ys) == pytest.approx(100, rel=0.02)
    holes = sorted(
        (f["params"]["diameter_mm"], f["params"]["center_x_mm"], f["params"]["center_y_mm"])
        for f in result.features
    )
    assert [d for d, _x, _y in holes] == [10.0, 16.0]
    assert holes[1][1] == pytest.approx(16, abs=1.0) and holes[1][2] == pytest.approx(14, abs=1.0)


def test_thickness_label_is_parsed():
    from app.ai.cad_views.labels import parse_label

    assert parse_label("s3*").kind == "thickness"
    assert parse_label("s 2,5").value == 2.5
