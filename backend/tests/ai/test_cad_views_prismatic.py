"""Метод `views`: призматическая деталь по трём видам (пересечение контуров)."""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")


def _bracket_sheet() -> np.ndarray:
    """Уголок: полка 60 (X) × 40 (Y) × 10 (Z) + стойка 10 × 40 × 30 слева.

    Главный вид (X, Z) — Г; под ним вид сверху (X, Y) — прямоугольник 60×40;
    справа вид слева (−Y, Z) — прямоугольник 40×30. 8 px/мм, основная 6 px.
    """
    k = 8.0
    sheet = np.full((1400, 1400), 255, np.uint8)
    fx, fy = 150, 450  # левый нижний угол главного вида

    def poly(points, ox, oy):
        pts = np.array([[int(ox + x * k), int(oy - y * k)] for x, y in points], np.int32)
        cv2.polylines(sheet, [pts], True, 0, 6)

    poly([(0, 0), (60, 0), (60, 10), (10, 10), (10, 30), (0, 30)], fx, fy)
    poly([(0, 0), (60, 0), (60, 40), (0, 40)], fx, fy + 100 + 40 * k)
    poly([(0, 0), (40, 0), (40, 30), (0, 30)], fx + 60 * k + 100, fy)
    return sheet


def test_bracket_is_the_intersection_of_its_views():
    from app.ai.cad_views.prismatic import build_prismatic
    from app.ai.cad_views.sheet_reading import Region, SheetReading

    sheet = _bracket_sheet()
    reading = SheetReading("detail", 1, [Region(1, (100, 150, 1300, 1300), "view", "главный вид")])
    result = build_prismatic(sheet, reading, ["60", "40", "30", "10"])
    assert result.ok, result.reason
    width, depth, height = result.profile["bounds_mm"]
    assert (width, depth, height) == pytest.approx((60, 40, 30), abs=0.6)
    kinds = [f["kind"] for f in result.candidate["candidate"]["features"]]
    assert kinds[0] == "extrude" and kinds.count("intersect") == 3
    main = next(f for f in result.candidate["candidate"]["features"] if f["kind"] == "intersect")
    assert main["params"]["normal"] == "y"
    xs = sorted({round(p[0]) for p in main["params"]["polygon_mm"]})
    assert xs[0] == 0 and xs[-1] == 60 and 10 in xs
