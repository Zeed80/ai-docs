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


def _sheet(main: int = MAIN, thin: int = THIN, lowered: float = 0.0) -> np.ndarray:
    # Размер как у листа: ядро поиска кромок (0,6 % меньшей стороны) должно
    # быть толще основной линии, как на настоящем листе при любом dpi.
    image = Image.new("L", (2800, 2000), 255)
    draw = ImageDraw.Draw(image)
    x = X0
    previous = 0.0
    for diameter, length in STEPS:
        r = diameter / 2 * PX
        x_end = x + length * PX
        # ``lowered`` — верхняя кромка средней ступени ниже на столько px:
        # кромка и опущенная кромка лыски, слитые в одну линию.
        top = AXIS - r + (lowered if (diameter, length) == STEPS[1] else 0.0)
        draw.line([(x, top), (x_end, top)], fill=0, width=main)
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


def test_a_wrong_link_of_the_chain_is_refuted_not_hidden_as_the_wrong_view():
    """shaft-1 вживую: 98 вместо 80 уводило масштаб из суммы на 6,7 % — «не тот вид»."""
    verdict = _check([(30.0, 30.0), (20.0, 40.0), (25.0, 36.0)])  # на листе 30

    assert verdict.status == "refuted", verdict.reason
    steps = verdict.measured["steps"]
    assert abs(steps[2]["length_mm"] - 30.0) <= 0.5, steps
    assert abs(steps[0]["diameter_mm"] - 30.0) <= 0.3, steps
    assert abs(steps[1]["length_mm"] - 40.0) <= 0.5, steps


def test_a_wrong_link_in_the_middle_keeps_the_links_after_it():
    """Неверное звено сдвигает станции за ним — граница за ним ищется от торца."""
    image = Image.new("L", (2800, 2000), 255)
    draw = ImageDraw.Draw(image)
    x, previous = X0, 0.0
    steps = [(30.0, 20.0), (20.0, 40.0), (25.0, 20.0), (18.0, 20.0)]
    for diameter, length in steps:
        r = diameter / 2 * PX
        x_end = x + length * PX
        draw.line([(x, AXIS - r), (x_end, AXIS - r)], fill=0, width=MAIN)
        draw.line([(x, AXIS + r), (x_end, AXIS + r)], fill=0, width=MAIN)
        low, high = min(previous, r), max(previous, r)
        if previous == 0.0:
            draw.line([(x, AXIS - r), (x, AXIS + r)], fill=0, width=MAIN)
        else:
            draw.line([(x, AXIS - high), (x, AXIS - low)], fill=0, width=MAIN)
            draw.line([(x, AXIS + low), (x, AXIS + high)], fill=0, width=MAIN)
        previous, x = r, x_end
    draw.line([(x, AXIS - previous), (x, AXIS + previous)], fill=0, width=MAIN)
    read = [(30.0, 20.0), (20.0, 48.0), (25.0, 20.0), (18.0, 20.0)]  # вторая — 40
    verdict = _verdict(read, sheet=np.asarray(image))

    assert verdict.status == "refuted", verdict.reason
    got = [step["length_mm"] for step in verdict.measured["steps"]]
    assert abs(got[1] - 40.0) <= 0.5, got
    assert abs(got[2] - 20.0) <= 0.5 and abs(got[3] - 20.0) <= 0.5, got


def test_an_end_face_continued_by_a_witness_line_is_still_an_end_face():
    """Контракт: выносная, продолжающая торец (размер фаски под видом), не мешает
    найти торец и масштаб.

    На корпусе v8 (shaft-5: полый вал, фаска 1 мм, выносные с обеих сторон)
    `_lines` склеивал торец с выносной, толщина у оси в столбце центра
    компоненты выходила 0, и вид не находился; исправлено `_at_rows`. Эта
    синтетика тот сбой НЕ воспроизводит (старый код её тоже проходит) —
    доказательство исправления корпусное: v8 shaft-5 и корпус v9."""
    image = Image.fromarray(_sheet()).copy()
    draw = ImageDraw.Draw(image)
    x_end = X0 + sum(length for _d, length in STEPS) * PX
    r_end = STEPS[-1][0] / 2 * PX
    # Тонкая выносная в 3 px от торца, от кромки вниз под вид.
    draw.line([(x_end + 3, AXIS + r_end), (x_end + 3, AXIS + r_end + 250)], fill=0, width=THIN)
    draw.line([(x_end + 3, AXIS - r_end - 120), (x_end + 3, AXIS - r_end)], fill=0, width=THIN)
    located = locate_shaft_frame(np.asarray(image), 100.0)

    assert located is not None
    frame, profile = located
    assert abs(profile.x1 - x_end) <= 3.0, profile.x1
    assert abs(frame.mm_per_px - 1 / PX) / (1 / PX) <= 0.01


def test_a_shifted_shoulder_is_refuted_with_the_measured_lengths():
    verdict = _check([(30.0, 32.0), (20.0, 38.0), (25.0, 30.0)])

    assert verdict.status == "refuted"
    assert verdict.measured["steps"][0]["length_mm"] is None or (
        abs(verdict.measured["steps"][0]["length_mm"] - 30.0) <= 0.5
    )


def test_the_main_view_is_the_one_whose_steps_match_the_sheet_diameters():
    """Живой part_01: проверку торцов прошла и рамка листа («Ø132»), и как
    самый длинный вид она выигрывала у вала."""
    from app.ai.cad_recognize.verifiers.shaft_frame import locate_shaft_views

    image = Image.fromarray(_sheet()).copy()
    draw = ImageDraw.Draw(image)
    # Рамка листа основной линией, на всю ширину и высоту.
    draw.rectangle([40, 60, 2760, 1940], outline=0, width=MAIN)
    sheet = np.asarray(image)
    total = sum(length for _d, length in STEPS)

    views = locate_shaft_views(sheet, total, [d for d, _l in STEPS])

    assert views, "вид не найден"
    _frame, profile = views[0]
    assert abs(profile.x0 - X0) <= 4 and abs(profile.axis_y - AXIS) <= 4, (
        profile.x0,
        profile.x1,
        profile.axis_y,
    )


def test_a_step_with_an_asymmetric_edge_inside_the_view_does_not_split_it():
    """Лыска на грубом исходнике: кромка ступени опущена на 3,5 px (слилась с
    кромкой лыски) — пары на всей ступени нет, но вид — целый, торцы — свои."""
    located = locate_shaft_frame(_sheet(lowered=3.5), sum(length for _d, length in STEPS))

    assert located is not None
    frame, _profile = located
    assert abs(frame.origin_px[0] - X0) <= 3
    assert abs(frame.mm_per_px - 1.0 / PX) <= 0.01 / PX


def test_the_view_is_found_the_same_on_a_sheet_scanned_at_a_higher_resolution():
    """Скан 600 dpi / лист после увеличения: вид вала и масштаб — те же.

    Допуск симметрии пары и порог торца были пиксельными: при ×2 кромки,
    несимметричные на 2 px при 300 dpi, теряли пару, и вид обрывался.
    """
    import cv2

    base = _sheet(lowered=1.5)
    total = sum(length for _d, length in STEPS)
    for factor in (1.5, 2.0):
        sheet = cv2.resize(base, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        located = locate_shaft_frame(sheet, total)
        assert located is not None, factor
        frame, _profile = located
        assert abs(frame.mm_per_px * factor - 1.0 / PX) <= 0.01 / PX, (factor, frame.mm_per_px)


def test_a_flat_lowering_one_edge_does_not_make_a_step():
    """Лыска опускает одну кромку силуэта: ступень — по целой кромке, а не
    Ø = 2R − глубина (turned_multiaxis-0: ложная ступень Ø23,7 посреди Ø25)."""
    from app.ai.cad_recognize.verifiers.sheet_profile import _main_steps

    located = locate_shaft_frame(_sheet(lowered=3.5), sum(length for _d, length in STEPS))
    frame, profile = located
    steps = _main_steps(profile)

    assert len(steps) == len(STEPS), steps
    middle = 2.0 * steps[1][2] * frame.mm_per_px
    assert abs(middle - STEPS[1][0]) < 0.5, middle
