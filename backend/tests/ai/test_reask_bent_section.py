"""Недостающая полка гнутой детали — переспрос по вырезу сечения (X4, Ф8)."""

from app.ai.cad_recognize.verifiers.reask import bent_flanges_fit

# Z-профиль 40 / 60 / 30 по наружной поверхности, s = 2: по осевой
# 39 / 58 / 29 мм, на листе 4 px/мм.
PX = [156.0, 232.0, 116.0]


def test_the_answer_that_fits_the_section_and_keeps_what_was_read_is_taken():
    assert bent_flanges_fit([40.0, 60.0, 30.0], PX, 2.0, [40.0, 60.0]) == [40.0, 60.0, 30.0]


def test_the_answer_read_from_the_other_end_is_put_in_the_sheet_order():
    assert bent_flanges_fit([30.0, 60.0, 40.0], PX, 2.0, [40.0, 60.0]) == [40.0, 60.0, 30.0]


def test_an_answer_that_contradicts_the_section_is_refused():
    # Третья полка вдвое длиннее, чем на листе.
    assert bent_flanges_fit([40.0, 60.0, 60.0], PX, 2.0, [40.0, 60.0]) is None


def test_proportional_numbers_without_the_flanges_already_read_are_refused():
    # Всё вдвое меньше — пропорции те же, но это не числа с листа.
    assert bent_flanges_fit([20.0, 30.0, 15.0], PX, 2.0, [40.0, 60.0]) is None


def test_a_wrong_count_is_refused():
    assert bent_flanges_fit([40.0, 60.0], PX, 2.0, [40.0, 60.0]) is None
