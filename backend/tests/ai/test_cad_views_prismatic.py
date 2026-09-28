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
    # Тело — по кромкам видов; вырез уступа — только у главного вида (Г),
    # вид сверху и вид слева — прямоугольники тела, им пересекать нечего.
    assert kinds[0] == "extrude" and kinds.count("intersect") == 1
    main = next(f for f in result.candidate["candidate"]["features"] if f["kind"] == "intersect")
    assert main["params"]["normal"] == "y"
    xs = sorted({round(p[0]) for p in main["params"]["polygon_mm"]})
    assert xs[0] == 0 and xs[-1] == 60 and 10 in xs


def _box_with_boss_sheet(section: bool = False) -> np.ndarray:
    """Брусок 60 (X) × 40 (Y) × 30 (Z) и прилив Ø16 × 6 на правой грани
    (центр y = 20, z = 15). ЕСКД: главный вид (X, Z), под ним вид сверху
    (X, Y), справа вид слева (−Y, Z). 8 px/мм, основная 6 px, тонкая 2 px.

    ``section`` — вид сверху заменён разрезом: полость 30 × 20 (X × Y) от
    верхней грани на глубину 20 (невидимым контуром на главном виде)."""
    k = 8.0
    sheet = np.full((1500, 1500), 255, np.uint8)
    fx, fy = 150, 450

    def rect(x0, y0, x1, y1, ox, oy, width=6):
        cv2.rectangle(
            sheet,
            (int(ox + x0 * k), int(oy - y1 * k)),
            (int(ox + x1 * k), int(oy - y0 * k)),
            0,
            width,
        )

    rect(0, 0, 60, 30, fx, fy)
    rect(60, 7, 66, 23, fx, fy)  # прилив сбоку на главном виде
    top_y = int(fy + 100 + 40 * k)
    rect(0, 0, 60, 40, fx, top_y)
    rect(60, 12, 66, 28, fx, top_y)  # тот же прилив на виде сверху
    rx = fx + 60 * k + 150
    rect(0, 0, 40, 30, rx, fy)
    cv2.circle(sheet, (int(rx + 20 * k), int(fy - 15 * k)), int(8 * k), 0, 2)  # за стенкой
    if section:
        # Вид сверху — разрез: штриховка под 45° везде в теле, кроме полости
        # 15..45 × 10..30 (X × Y), полость открыта на верхнюю грань.
        hatch = np.full_like(sheet, 255)
        for c in range(-1500, 1500, 24):
            cv2.line(hatch, (c, 1500), (c + 1500, 0), 0, 2)
        body = np.zeros_like(sheet, dtype=bool)
        body[int(top_y - 40 * k) : top_y, fx : int(fx + 60 * k)] = True
        body[int(top_y - 30 * k) : int(top_y - 10 * k), int(fx + 15 * k) : int(fx + 45 * k)] = False
        sheet[body & (hatch == 0)] = 0
        rect(15, 10, 45, 30, fx, top_y)
        # На главном виде полость — невидимый контур от верхней грани на 20.
        z_bottom = int(fy - 10 * k)
        for x in range(int(fx + 15 * k), int(fx + 45 * k), 40):
            cv2.line(sheet, (x, z_bottom), (x + 24, z_bottom), 0, 2)
        for z in range(int(fy - 30 * k) + 8, z_bottom, 40):
            for x in (int(fx + 15 * k), int(fx + 45 * k)):
                cv2.line(sheet, (x, z), (x, z + 24), 0, 2)
    return sheet


def test_boss_is_built_from_two_views_and_its_circle():
    from app.ai.cad_views.prismatic import build_prismatic
    from app.ai.cad_views.sheet_reading import Region, SheetReading

    sheet = _box_with_boss_sheet()
    reading = SheetReading("detail", 1, [Region(1, (50, 50, 1450, 1450), "view", "главный вид")])
    result = build_prismatic(sheet, reading, ["60", "40", "30", "Ø16", "6"])
    assert result.ok, result.reason
    assert result.profile["body_mm"] == pytest.approx([60, 40, 30], abs=0.5)
    bosses = [f for f in result.candidate["candidate"]["features"] if f["kind"] == "boss"]
    assert len(bosses) == 1
    params = bosses[0]["params"]
    assert params["profile"] == "circle" and params["diameter_mm"] == pytest.approx(16, abs=0.5)
    assert params["depth_mm"] == pytest.approx(6, abs=0.6)
    assert params["placement"]["axis"] == [1.0, 0.0, 0.0]
    origin = params["placement"]["origin"]
    assert origin[0] == pytest.approx(60, abs=0.5)
    assert origin[1] == pytest.approx(20, abs=0.8) and origin[2] == pytest.approx(15, abs=0.8)


def test_cavity_comes_from_the_section_and_the_hidden_outline():
    from app.ai.cad_views.prismatic import build_prismatic
    from app.ai.cad_views.sheet_reading import Region, SheetReading

    sheet = _box_with_boss_sheet(section=True)
    reading = SheetReading("detail", 1, [Region(1, (50, 50, 1450, 1450), "view", "главный вид")])
    result = build_prismatic(sheet, reading, ["60", "40", "30", "Ø16", "6", "20", "15"])
    assert result.ok, result.reason
    cuts = [f for f in result.candidate["candidate"]["features"] if f["kind"] == "cut_prism"]
    assert len(cuts) == 1
    params = cuts[0]["params"]
    assert params["normal"] == "z"
    xs = sorted(p[0] for p in params["polygon_mm"])
    ys = sorted(p[1] for p in params["polygon_mm"])
    assert xs[0] == pytest.approx(15, abs=0.8) and xs[-1] == pytest.approx(45, abs=0.8)
    assert ys[0] == pytest.approx(10, abs=0.8) and ys[-1] == pytest.approx(30, abs=0.8)
    # Полость от верхней грани (z = 30) вниз до z = 10.
    assert params["range_mm"] == pytest.approx([10, 30], abs=1.0)
