"""Шпоночный паз по главному виду вала: капсула — начало, длина, ширина."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers import Hypothesis, verify
from app.ai.cad_recognize.verifiers.shaft_frame import locate_shaft_frame, locate_shaft_views

# Вал 30×30 → Ø20×40 → Ø25×30 при 5 px/мм; левый торец x=200, ось y=500.
PX = 5.0
X0, AXIS = 200.0, 500.0
STEPS = [(30.0, 30.0), (20.0, 40.0), (25.0, 30.0)]
MAIN, THIN = 6, 3
# Паз на средней ступени: 40…60 мм, ширина 6.
KEY = {"axial_start_mm": 40.0, "length_mm": 20.0, "width_mm": 6.0}
_KEY_ARGS = (KEY["axial_start_mm"], KEY["length_mm"], KEY["width_mm"])


def _shaft(draw: ImageDraw.ImageDraw, main: int, axis: float = AXIS) -> float:
    x, previous = X0, 0.0
    for diameter, length in STEPS:
        r = diameter / 2 * PX
        x_end = x + length * PX
        draw.line([(x, axis - r), (x_end, axis - r)], fill=0, width=main)
        draw.line([(x, axis + r), (x_end, axis + r)], fill=0, width=main)
        low, high = min(previous, r), max(previous, r)
        if previous == 0.0:
            draw.line([(x, axis - r), (x, axis + r)], fill=0, width=main)
        else:
            draw.line([(x, axis - high), (x, axis - low)], fill=0, width=main)
            draw.line([(x, axis + low), (x, axis + high)], fill=0, width=main)
        previous, x = r, x_end
    draw.line([(x, axis - previous), (x, axis + previous)], fill=0, width=main)
    return x


def _capsule(
    draw: ImageDraw.ImageDraw,
    start: float,
    length: float,
    width: float,
    main: int,
    axis: float = AXIS,
):
    h = width / 2 * PX
    left = X0 + (start + width / 2) * PX
    right = X0 + (start + length - width / 2) * PX
    draw.line([(left, axis - h), (right, axis - h)], fill=0, width=main)
    draw.line([(left, axis + h), (right, axis + h)], fill=0, width=main)
    # Обводка серединой на радиусе (PIL кладёт её внутрь рамки).
    r = h + main / 2
    draw.arc([left - r, axis - r, left + r, axis + r], 90, 270, fill=0, width=main)
    draw.arc([right - r, axis - r, right + r, axis + r], -90, 90, fill=0, width=main)


def _hatched_bore(draw: ImageDraw.ImageDraw, axis: float) -> None:
    """Разрез полого вала: «расточка» с дугами, за её линиями — штриховка 45°."""
    x_end = X0 + sum(length for _d, length in STEPS) * PX
    for offset in range(-100, 600, 12):
        draw.line([(X0 + offset, axis - 49), (X0 + offset + 98, axis + 49)], fill=0, width=2)
    # Штриховка кончается на контуре детали, внутри «расточки» пусто.
    draw.rectangle([0, axis - 50, X0, axis + 50], fill=255)
    draw.rectangle([x_end, axis - 50, x_end + 200, axis + 50], fill=255)
    draw.rectangle([X0 + 40 * PX, axis - 12, X0 + 60 * PX, axis + 12], fill=255)
    _shaft(draw, MAIN, axis)
    _capsule(draw, *_KEY_ARGS, main=MAIN, axis=axis)


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
    _hatched_bore(ImageDraw.Draw(image), AXIS)
    verdict = _verdict(KEY, np.asarray(image))

    assert verdict.status == "unmeasurable", (verdict.reason, verdict.measured)


def test_a_hollow_shaft_keyway_is_checked_on_the_view_below_the_section():
    """shaft-8/12: опорный вид полого вала — разрез, паз лицом — на виде под ним."""
    from app.ai.cad_recognize.verifiers.stage import _keyways

    below = AXIS + 700.0
    image = Image.new("L", (2800, 2000), 255)
    draw = ImageDraw.Draw(image)
    _hatched_bore(draw, AXIS)
    _shaft(draw, MAIN, below)
    _capsule(draw, *_KEY_ARGS, main=MAIN, axis=below)
    gray = np.asarray(image)
    views = [frame for frame, _profile in locate_shaft_views(gray, 100.0)]
    section = [frame for frame in views if abs(frame.origin_px[1] - AXIS) <= 2.0]
    face_on = [frame for frame in views if abs(frame.origin_px[1] - below) <= 2.0]
    assert section and face_on, [frame.origin_px for frame in views]

    report: dict = {"items": [], "notes": []}
    # Разрез — первым, как у полого вала на корпусе.
    _keyways(gray, section + face_on, {"keyways": [{**KEY, "depth_mm": 3.5}]}, report)

    item = report["items"][0]
    assert item["status"] == "confirmed", item
    assert abs(item["measured"]["length_mm"] - 20.0) <= 0.5


def test_a_coarse_sheet_is_unmeasurable_not_a_guess():
    verdict = _verdict(KEY, _sheet(main=3, thin=1))

    assert verdict.status == "unmeasurable"
    assert "грубый" in verdict.reason


def test_without_the_shaft_view_the_keyway_is_unmeasurable():
    verdict = verify(Hypothesis("keyway", "main_view.keyways[0]", KEY), None, _sheet())

    assert verdict.status == "unmeasurable"
