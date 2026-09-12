"""Проверяльщик отверстия пластины: прочитанный x выбирает окружность."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers import Hypothesis, ViewFrame, verify

# План пластины 80×50 мм при 10 px/мм; левый нижний угол — (100, 600) px.
PX_PER_MM = 10.0
ORIGIN = (100.0, 600.0)
HOLES = [(18.0, 23.0, 5.5), (39.0, 19.0, 11.0), (52.0, 32.0, 6.6), (64.0, 26.0, 5.5)]


def _sheet() -> np.ndarray:
    image = Image.new("L", (1000, 800), 255)
    draw = ImageDraw.Draw(image)
    x0, y0 = ORIGIN
    draw.rectangle([x0, y0 - 50 * PX_PER_MM, x0 + 80 * PX_PER_MM, y0], outline=0, width=5)
    for x, y, d in HOLES:
        cx, cy, r = x0 + x * PX_PER_MM, y0 - y * PX_PER_MM, d / 2 * PX_PER_MM
        # Обводка — серединой на радиусе, как на настоящем чертеже: PIL рисует
        # её внутрь рамки, поэтому рамка шире на половину толщины линии.
        w = 5
        draw.ellipse(
            [cx - r - w / 2, cy - r - w / 2, cx + r + w / 2, cy + r + w / 2], outline=0, width=w
        )
        # выносные координат идут через центр — как на листе
        draw.line([(cx, cy), (cx, 40)], fill=0, width=2)
        draw.line([(cx, cy), (20, cy)], fill=0, width=2)
        draw.line([(cx - r, cy + r), (cx + r, cy - r)], fill=0, width=2)
        # стрелки диаметрального размера — залитые, остриями на окружности
        k = r / 1.414
        for sx, sy in ((1, -1), (-1, 1)):
            tip = (cx + sx * k, cy + sy * k)
            back = (cx + sx * (k - 30), cy + sy * (k - 30))
            draw.polygon([tip, (back[0] - 8, back[1] - 8), (back[0] + 8, back[1] + 8)], fill=0)
    return np.asarray(image)


FRAME = ViewFrame(bbox_px=(80, 80, 920, 620), mm_per_px=1 / PX_PER_MM, origin_px=ORIGIN)


def _check(x, y, d):
    return verify(
        Hypothesis("plate_hole", "holes/0", {"x_mm": x, "y_mm": y, "diameter_mm": d}),
        FRAME,
        _sheet(),
    )


def test_a_correctly_read_hole_is_confirmed():
    for x, y, d in HOLES:
        verdict = _check(x, y, d)
        assert verdict.status == "confirmed", (x, y, d, verdict.reason, verdict.measured)


def test_swapped_y_is_refuted_with_the_true_y_measured():
    """plate-1: x прочитаны верно, y переставлены парами."""
    verdict = _check(52.0, 26.0, 6.6)  # y от соседнего отверстия

    assert verdict.status == "refuted"
    assert abs(verdict.measured["y_mm"] - 32.0) <= 0.3
    assert abs(verdict.measured["diameter_mm"] - 6.6) <= 0.3


def test_a_wrong_diameter_is_refuted_with_the_true_one_measured():
    verdict = _check(39.0, 19.0, 6.5)  # plate-1: Ø11 прочитан как 6,5

    assert verdict.status == "refuted"
    assert abs(verdict.measured["diameter_mm"] - 11.0) <= 0.3


def test_no_hole_at_the_read_x_is_unmeasurable_not_a_guess():
    verdict = _check(30.0, 10.0, 5.5)
    assert verdict.status == "unmeasurable"


def test_a_corner_fillet_arc_next_to_a_corner_hole_is_not_taken_for_it():
    """plate-13 корпуса: угловое отверстие Ø9 измерилось как Ø17 — дуга скругления."""
    image = Image.fromarray(_sheet())
    draw = ImageDraw.Draw(image)
    x0, y0 = ORIGIN
    # скругление R10 у левого нижнего угла и угловое отверстие Ø9 в (7, 7)
    draw.arc([x0, y0 - 200, x0 + 200, y0], start=90, end=180, fill=0, width=5)
    cx, cy, r = x0 + 70, y0 - 70, 45
    draw.ellipse([cx - r - 2.5, cy - r - 2.5, cx + r + 2.5, cy + r + 2.5], outline=0, width=5)
    verdict = verify(
        Hypothesis("plate_hole", "h", {"x_mm": 7.0, "y_mm": 7.0, "diameter_mm": 9.0}),
        FRAME,
        np.asarray(image),
    )

    assert verdict.status == "confirmed", (verdict.reason, verdict.measured)
