"""Шпоночный паз по главному виду вала: капсула — начало, длина, ширина."""

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
# Паз на средней ступени: 40…60 мм, ширина 6.
KEY = {"axial_start_mm": 40.0, "length_mm": 20.0, "width_mm": 6.0}
_KEY_ARGS = (KEY["axial_start_mm"], KEY["length_mm"], KEY["width_mm"])


def _shaft(draw: ImageDraw.ImageDraw, main: int) -> float:
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
    return x


def _capsule(draw: ImageDraw.ImageDraw, start: float, length: float, width: float, main: int):
    h = width / 2 * PX
    left = X0 + (start + width / 2) * PX
    right = X0 + (start + length - width / 2) * PX
    draw.line([(left, AXIS - h), (right, AXIS - h)], fill=0, width=main)
    draw.line([(left, AXIS + h), (right, AXIS + h)], fill=0, width=main)
    # Обводка серединой на радиусе (PIL кладёт её внутрь рамки).
    r = h + main / 2
    draw.arc([left - r, AXIS - r, left + r, AXIS + r], 90, 270, fill=0, width=main)
    draw.arc([right - r, AXIS - r, right + r, AXIS + r], -90, 90, fill=0, width=main)


def _sheet(*, main: int = MAIN, thin: int = THIN, label: bool = False) -> np.ndarray:
    image = Image.new("L", (2800, 2000), 255)
    draw = ImageDraw.Draw(image)
    x_end = _shaft(draw, main)
    _capsule(draw, *_KEY_ARGS, main=main)
    for dy in (160, 200):
        draw.line([(X0, AXIS - dy), (x_end, AXIS - dy)], fill=0, width=thin)
        draw.line([(X0, AXIS + dy), (x_end, AXIS + dy)], fill=0, width=thin)
    if label:
        # Размер Ø ступени поперёк паза: размерная линия и подпись («Ø20»
        # повёрнута вдоль линии) — пятно, которое рвёт прямые паза пополам.
        middle = X0 + 50.0 * PX
        draw.line([(middle, AXIS - 50), (middle, AXIS + 50)], fill=0, width=thin)
        draw.rectangle([middle - 22, AXIS - 30, middle - 6, AXIS + 30], fill=0)
    return np.asarray(image)


def _verdict(read: dict, sheet: np.ndarray | None = None):
    sheet = _sheet() if sheet is None else sheet
    located = locate_shaft_frame(sheet, sum(length for _d, length in STEPS))
    frame = located[0] if located else None
    return verify(Hypothesis("keyway", "main_view.keyways[0]", read), frame, sheet)


def test_a_correct_keyway_is_confirmed_with_the_measured_size():
    verdict = _verdict(KEY)

    assert verdict.status == "confirmed", (verdict.reason, verdict.measured)
    for field, tolerance in (("axial_start_mm", 0.5), ("length_mm", 0.5), ("width_mm", 0.3)):
        assert abs(verdict.measured[field] - KEY[field]) <= tolerance, verdict.measured


def test_a_keyway_split_by_the_diameter_label_is_measured_whole():
    """shaft-6, shaft-28: подпись Ø рвала прямые, мерилась половина паза."""
    verdict = _verdict(KEY, _sheet(label=True))

    assert verdict.status == "confirmed", (verdict.reason, verdict.measured)
    assert abs(verdict.measured["length_mm"] - 20.0) <= 0.5


def test_a_wrong_start_and_a_wrong_length_are_refuted_with_the_sheet_values():
    for field, delta in (("axial_start_mm", 3.0), ("length_mm", 5.0), ("width_mm", 2.0)):
        verdict = _verdict({**KEY, field: KEY[field] + delta}, _sheet(label=True))

        assert verdict.status == "refuted", (field, verdict.measured)
        # Замер — с листа, а не подогнан под прочитанное.
        assert abs(verdict.measured[field] - KEY[field]) <= 0.5, (field, verdict.measured)


def test_a_bore_in_a_hatched_section_is_not_a_keyway():
    """shaft-8: расточка разреза — те же прямые с дугами, но за ними штриховка."""
    image = Image.new("L", (2800, 2000), 255)
    draw = ImageDraw.Draw(image)
    _shaft(draw, MAIN)
    _capsule(draw, *_KEY_ARGS, main=MAIN)
    # Штриховка под 45° между «расточкой» и кромкой ступени.
    for offset in range(-400, 900, 12):
        draw.line([(X0 + offset, AXIS - 49), (X0 + offset + 98, AXIS + 49)], fill=0, width=2)
    # Внутри «расточки» пусто, штриховка начинается сразу за её линией.
    draw.rectangle([X0 + 40 * PX, AXIS - 12, X0 + 60 * PX, AXIS + 12], fill=255)
    _capsule(draw, *_KEY_ARGS, main=MAIN)
    verdict = _verdict(KEY, np.asarray(image))

    assert verdict.status == "unmeasurable", (verdict.reason, verdict.measured)


def test_a_coarse_sheet_is_unmeasurable_not_a_guess():
    verdict = _verdict(KEY, _sheet(main=3, thin=1))

    assert verdict.status == "unmeasurable"
    assert "грубый" in verdict.reason


def test_without_the_shaft_view_the_keyway_is_unmeasurable():
    verdict = verify(Hypothesis("keyway", "main_view.keyways[0]", KEY), None, _sheet())

    assert verdict.status == "unmeasurable"
