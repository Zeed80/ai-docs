"""Поперечное отверстие вала: окружность на оси — положение и Ø."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers import Hypothesis, verify
from app.ai.cad_recognize.verifiers.shaft_frame import locate_shaft_frame

# Вал 30×30 → Ø20×40 → Ø25×30 при 5 px/мм; левый торец x=200, ось y=500.
PX = 5.0
X0, AXIS = 200.0, 500.0
STEPS = [(30.0, 30.0), (20.0, 40.0), (25.0, 30.0)]
MAIN, THIN = 6, 3
# Отверстие Ø6 на средней ступени, на 50 мм от левого торца.
HOLE = {"axial_position_mm": 50.0, "diameter_mm": 6.0}


def _sheet(*, main: int = MAIN, hole: bool = True) -> np.ndarray:
    image = Image.new("L", (2800, 2000), 255)
    draw = ImageDraw.Draw(image)
    x, previous = X0, 0.0
    for diameter, length in STEPS:
        r = diameter / 2 * PX
        x_end = x + length * PX
        draw.line([(x, AXIS - r), (x_end, AXIS - r)], fill=0, width=main)
        draw.line([(x, AXIS + r), (x_end, AXIS + r)], fill=0, width=main)
        low, high = min(previous, r), max(previous, r)
        if previous == 0.0:
            draw.line([(x, AXIS - r), (x, AXIS + r)], fill=0, width=main)
        else:
            draw.line([(x, AXIS - high), (x, AXIS - low)], fill=0, width=main)
            draw.line([(x, AXIS + low), (x, AXIS + high)], fill=0, width=main)
        previous, x = r, x_end
    draw.line([(x, AXIS - previous), (x, AXIS + previous)], fill=0, width=main)
    if hole:
        cx = X0 + HOLE["axial_position_mm"] * PX
        # Обводка серединой на радиусе (PIL кладёт её внутрь рамки).
        r = HOLE["diameter_mm"] / 2 * PX + main / 2
        draw.ellipse([cx - r, AXIS - r, cx + r, AXIS + r], outline=0, width=main)
    return np.asarray(image)


def _verdict(read: dict, sheet: np.ndarray | None = None):
    sheet = _sheet() if sheet is None else sheet
    located = locate_shaft_frame(sheet, sum(length for _d, length in STEPS))
    frame = located[0] if located else None
    return verify(Hypothesis("cross_hole", "main_view.cross_holes[0]", read), frame, sheet)


def test_a_correct_cross_hole_is_confirmed_with_the_measured_values():
    verdict = _verdict(HOLE)

    assert verdict.status == "confirmed", (verdict.reason, verdict.measured)
    assert abs(verdict.measured["axial_position_mm"] - 50.0) <= 0.5
    assert abs(verdict.measured["diameter_mm"] - 6.0) <= 0.3


def test_a_wrong_position_or_diameter_is_refuted_with_the_sheet_values():
    for field, delta in (("axial_position_mm", 3.0), ("diameter_mm", 2.0)):
        verdict = _verdict({**HOLE, field: HOLE[field] + delta})

        assert verdict.status == "refuted", (field, verdict.measured)
        assert abs(verdict.measured[field] - HOLE[field]) <= 0.5, (field, verdict.measured)


def test_without_a_circle_on_the_axis_the_hole_is_unmeasurable_not_refuted():
    verdict = _verdict(HOLE, _sheet(hole=False))

    assert verdict.status == "unmeasurable", verdict.measured


def test_a_coarse_sheet_is_unmeasurable_not_a_guess():
    verdict = _verdict(HOLE, _sheet(main=3))

    assert verdict.status == "unmeasurable"
