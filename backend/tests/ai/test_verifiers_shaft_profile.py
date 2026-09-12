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
# Основная и тонкая линии — как 0,5 и 0,25 мм при 300 dpi: проверка профиля
# требует такого разрешения (грубее — «не измеримо», см. тест ниже).
MAIN, THIN = 6, 3


def _sheet(main: int = MAIN, thin: int = THIN) -> np.ndarray:
    # Размер как у листа: ядро поиска кромок (0,6 % меньшей стороны) должно
    # быть толще основной линии, как на настоящем листе при любом dpi.
    image = Image.new("L", (2800, 2000), 255)
    draw = ImageDraw.Draw(image)
    x = X0
    previous = 0.0
    for diameter, length in STEPS:
        r = diameter / 2 * PX
        x_end = x + length * PX
        draw.line([(x, AXIS - r), (x_end, AXIS - r)], fill=0, width=main)
        draw.line([(x, AXIS + r), (x_end, AXIS + r)], fill=0, width=main)
        # грань уступа / торец — от меньшего радиуса до большего
        low = min(previous, r)
        high = max(previous, r)
        if previous == 0.0:
            draw.line([(x, AXIS - r), (x, AXIS + r)], fill=0, width=main)
        else:
            draw.line([(x, AXIS - high), (x, AXIS - low)], fill=0, width=main)
            draw.line([(x, AXIS + low), (x, AXIS + high)], fill=0, width=main)
        previous, x = r, x_end
    draw.line([(x, AXIS - previous), (x, AXIS + previous)], fill=0, width=main)
    # Приманки: тонкие размерные линии, симметричные оси, шире детали, и вид
    # с торца на той же оси.
    for dy in (160, 200):
        draw.line([(X0, AXIS - dy), (x, AXIS - dy)], fill=0, width=thin)
        draw.line([(X0, AXIS + dy), (x, AXIS + dy)], fill=0, width=thin)
    draw.ellipse([1150, AXIS - 75, 1300, AXIS + 75], outline=0, width=main)
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


def _verdict(steps, *, sheet=None, keyways=()):
    sheet = _sheet() if sheet is None else sheet
    located = locate_shaft_frame(sheet, sum(length for _d, length in steps))
    frame, profile = located if located else (None, None)
    return verify(
        Hypothesis(
            "shaft_profile",
            "main_view.outer",
            {
                "steps": [{"diameter_mm": d, "length_mm": length} for d, length in steps],
                "keyways": list(keyways),
            },
        ),
        frame,
        profile,
    )


def test_a_coarse_sheet_is_unmeasurable_not_a_guess():
    """150 dpi на корпусе — 18–25 % ложных опровержений верного чтения."""
    verdict = _verdict(STEPS, sheet=_sheet(main=3, thin=1))

    assert verdict.status == "unmeasurable"
    assert "грубый" in verdict.reason


def test_a_step_under_a_keyway_is_not_measured_and_not_refuted():
    """Под пазом ступень меряется по контуру паза (shaft-12: Ø 19,56 вместо 20)."""
    verdict = _verdict(
        [(30.0, 30.0), (24.0, 40.0), (25.0, 30.0)],  # Ø средней прочитан неверно…
        keyways=[{"axial_start_mm": 40.0, "length_mm": 20.0}],  # …но она под пазом
    )

    assert verdict.status == "confirmed", verdict.reason
    assert verdict.measured["steps"][1]["diameter_mm"] is None


def test_a_profile_that_disagrees_almost_everywhere_is_unmeasurable():
    """Не тот вид или неверная система координат — не повод опровергать всё."""
    verdict = _verdict([(40.0, 30.0), (30.0, 40.0), (35.0, 30.0)])

    assert verdict.status == "unmeasurable"
    assert "не тот вид" in verdict.reason


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
