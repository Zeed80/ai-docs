"""Окружность болтов фланца: система координат по контуру и проверка массива."""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers import Hypothesis, verify
from app.ai.cad_recognize.verifiers.circle_frame import locate_circle_frame

# Фланец Ø100 при 6 px/мм: контур r=300 px в (600, 500); окружность центров
# Ø60 (r=180), 6 отверстий Ø6,6 (r≈20) с фазой 15°, расточка Ø25.
PX = 6.0
CX, CY = 600.0, 500.0
D, PCD, HOLE, COUNT, PHASE = 100.0, 60.0, 6.6, 6, 15.0


def _ring(draw, cx, cy, r, width):
    # Обводка — серединой на радиусе (PIL кладёт её внутрь рамки).
    draw.ellipse(
        [cx - r - width / 2, cy - r - width / 2, cx + r + width / 2, cy + r + width / 2],
        outline=0,
        width=width,
    )


def _sheet() -> np.ndarray:
    image = Image.new("L", (1300, 1000), 255)
    draw = ImageDraw.Draw(image)
    _ring(draw, CX, CY, D / 2 * PX, 4)  # контур — основная
    _ring(draw, CX, CY, PCD / 2 * PX, 2)  # окружность центров — тонкая
    _ring(draw, CX, CY, 25 / 2 * PX, 4)  # расточка
    for index in range(COUNT):
        angle = math.radians(PHASE + 360.0 / COUNT * index)
        hx, hy = CX + PCD / 2 * PX * math.cos(angle), CY - PCD / 2 * PX * math.sin(angle)
        _ring(draw, hx, hy, HOLE / 2 * PX, 4)
    # Приманки: петли цифр вне кольца и размерная линия через центр.
    for x, y in ((380, 260), (820, 740), (560, 330)):
        _ring(draw, x, y, 16, 2)
    draw.line([(CX - 300 * 0.7, CY + 300 * 0.7), (CX + 300 * 0.7, CY - 300 * 0.7)], fill=0, width=2)
    return np.asarray(image)


def _check(**changes):
    expected = {"count": COUNT, "pcd_mm": PCD, "hole_diameter_mm": HOLE, "start_angle_deg": PHASE}
    expected.update(changes)
    sheet = _sheet()
    return verify(
        Hypothesis("bolt_circle", "main_view.profile.hole_patterns[0]", expected),
        locate_circle_frame(sheet, D),
        sheet,
    )


def test_the_flange_outline_gives_the_centre_and_the_scale():
    frame = locate_circle_frame(_sheet(), D)

    assert frame is not None
    assert abs(frame.origin_px[0] - CX) <= 1.0 and abs(frame.origin_px[1] - CY) <= 1.0
    assert abs(frame.mm_per_px - 1 / PX) / (1 / PX) <= 0.01  # контур, а не окружность центров


def test_a_correct_read_is_confirmed():
    verdict = _check()

    assert verdict.status == "confirmed", (verdict.reason, verdict.measured)
    assert verdict.measured["count"] == COUNT


def test_a_zero_phase_the_sheet_does_not_carry_is_refuted_with_the_measured_phase():
    """Углового размера на листе нет, ридер пишет 0° — фазу меряет проверка."""
    verdict = _check(start_angle_deg=0.0)

    assert verdict.status == "refuted"
    assert abs(verdict.measured["start_angle_deg"] - PHASE) <= 1.0


def test_a_wrong_hole_count_is_refuted():
    verdict = _check(count=8)

    assert verdict.status == "refuted"
    assert verdict.measured["count"] == COUNT
