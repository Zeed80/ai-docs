"""Наружный профиль вала по листу: ось, торцы, Ø и длины ступеней."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers import Hypothesis, verify
from app.ai.cad_recognize.verifiers.shaft_frame import locate_shaft_frame

# Вал 30×30 → Ø20×40 → Ø25×30 при 5 px/мм; левый торец x=200, ось y=500.
PX = 5.0
X0, AXIS = 200.0, 500.0
STEPS = [(30.0, 30.0), (20.0, 40.0), (25.0, 30.0)]
MAIN, THIN = 4, 2


def _sheet() -> np.ndarray:
    image = Image.new("L", (1400, 1000), 255)
    draw = ImageDraw.Draw(image)
    x = X0
    previous = 0.0
    for diameter, length in STEPS:
        r = diameter / 2 * PX
        x_end = x + length * PX
        draw.line([(x, AXIS - r), (x_end, AXIS - r)], fill=0, width=MAIN)
        draw.line([(x, AXIS + r), (x_end, AXIS + r)], fill=0, width=MAIN)
        # грань уступа / торец — от меньшего радиуса до большего
        low = min(previous, r)
        high = max(previous, r)
        if previous == 0.0:
            draw.line([(x, AXIS - r), (x, AXIS + r)], fill=0, width=MAIN)
        else:
            draw.line([(x, AXIS - high), (x, AXIS - low)], fill=0, width=MAIN)
            draw.line([(x, AXIS + low), (x, AXIS + high)], fill=0, width=MAIN)
        previous, x = r, x_end
    draw.line([(x, AXIS - previous), (x, AXIS + previous)], fill=0, width=MAIN)
    # Приманки: тонкие размерные линии, симметричные оси, шире детали, и вид
    # с торца на той же оси.
    for dy in (160, 200):
        draw.line([(X0, AXIS - dy), (x, AXIS - dy)], fill=0, width=THIN)
        draw.line([(X0, AXIS + dy), (x, AXIS + dy)], fill=0, width=THIN)
    draw.ellipse([1150, AXIS - 75, 1300, AXIS + 75], outline=0, width=MAIN)
    return np.asarray(image)


def _check(steps):
    sheet = _sheet()
    located = locate_shaft_frame(sheet, sum(length for _d, length in steps))
    frame, profile = located if located else (None, None)
    return verify(
        Hypothesis(
            "shaft_profile",
            "main_view.outer",
            {"steps": [{"diameter_mm": d, "length_mm": length} for d, length in steps]},
        ),
        frame,
        profile,
    )


def test_the_axis_the_end_faces_and_the_scale_are_found_on_the_sheet():
    located = locate_shaft_frame(_sheet(), 100.0)

    assert located is not None
    frame, _profile = located
    assert abs(frame.origin_px[1] - AXIS) <= 1.0
    assert abs(frame.origin_px[0] - X0) <= 2.0
    assert abs(frame.mm_per_px - 1 / PX) / (1 / PX) <= 0.01


def test_a_correct_read_is_confirmed_and_thin_symmetric_lines_are_not_the_profile():
    verdict = _check(STEPS)

    assert verdict.status == "confirmed", (verdict.reason, verdict.measured)
    measured = [step["diameter_mm"] for step in verdict.measured["steps"]]
    assert all(abs(got - d) <= 0.3 for got, (d, _l) in zip(measured, STEPS))


def test_a_wrong_step_diameter_is_refuted_with_the_measured_one():
    verdict = _check([(30.0, 30.0), (22.0, 40.0), (25.0, 30.0)])

    assert verdict.status == "refuted"
    assert abs(verdict.measured["steps"][1]["diameter_mm"] - 20.0) <= 0.3


def test_a_shifted_shoulder_is_refuted_with_the_measured_lengths():
    verdict = _check([(30.0, 32.0), (20.0, 38.0), (25.0, 30.0)])

    assert verdict.status == "refuted"
    assert verdict.measured["steps"][0]["length_mm"] is None or (
        abs(verdict.measured["steps"][0]["length_mm"] - 30.0) <= 0.5
    )
