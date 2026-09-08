"""Обрезка листа: внутри границ, либо ничего — но никогда перевёрнутая.

Живой `detal_126.png` падал на `ValueError: Coordinate 'lower' is less than
'upper'`. Боксы контактных листов зажимались с ОДНОЙ стороны: `max(0, y - h)`
сверху и `min(image.height, y + h)` снизу. Пока центр лежит на листе, этого
достаточно — и на всех чертежах, на которых конвейер проверяли, он там лежал.

Но центр вычисляется из прочитанного масштаба и диаметра. Достаточно одному из
них разъехаться, чтобы `y + h` ушёл в минус: нижняя граница `min(height, -12)`
становится ВЫШЕ верхней, и PIL справедливо отказывается. Падал при этом весь
прогон — из-за картинки, которая была лишь свидетельством к одной гипотезе.

Тот же односторонний зажим стоял в семи местах: чинить надо семейство, а не
вызов, на котором повезло споткнуться.
"""

from __future__ import annotations

import pytest

from app.ai.cad_recognize.spec_fragments import _sheet_crop_box


class _Sheet:
    width, height = 800, 600


@pytest.fixture
def sheet() -> _Sheet:
    return _Sheet()


def test_a_box_inside_the_sheet_is_returned_as_is(sheet):
    assert _sheet_crop_box(sheet, (10, 20, 110, 120)) == (10, 20, 110, 120)


def test_a_box_hanging_off_an_edge_is_clamped_to_the_sheet(sheet):
    assert _sheet_crop_box(sheet, (-40, -30, 200, 150)) == (0, 0, 200, 150)
    assert _sheet_crop_box(sheet, (700, 500, 900, 700)) == (700, 500, 800, 600)


def test_a_region_entirely_above_the_sheet_yields_nothing(sheet):
    """Ровно случай падения: центр ушёл выше листа, `y + h` отрицателен."""
    assert _sheet_crop_box(sheet, (100, -260, 300, -12)) is None


def test_a_region_entirely_past_the_far_edge_yields_nothing(sheet):
    assert _sheet_crop_box(sheet, (900, 100, 1200, 300)) is None


def test_an_inverted_box_is_not_crashed_on_but_ordered(sheet):
    assert _sheet_crop_box(sheet, (300, 400, 100, 200)) == (100, 200, 300, 400)


def test_a_degenerate_sliver_is_nothing_rather_than_an_empty_image(sheet):
    """Белый прямоугольник в роли свидетельства хуже отсутствия свидетельства."""
    assert _sheet_crop_box(sheet, (100, 100, 101, 300)) is None


def test_no_sheet_means_no_box(sheet):
    assert _sheet_crop_box(None, (0, 0, 10, 10)) is None


def test_the_result_always_orders_correctly_whatever_it_is_given(sheet):
    """Контракт целиком: что бы ни пришло, PIL это примет либо не увидит."""
    extremes = (-5000.0, -12.0, 0.0, 7.5, 599.0, 800.0, 5000.0)
    for left in extremes:
        for right in extremes:
            for top in extremes:
                for bottom in extremes:
                    box = _sheet_crop_box(sheet, (left, top, right, bottom))
                    if box is None:
                        continue
                    x0, y0, x1, y1 = box
                    assert 0 <= x0 < x1 <= sheet.width
                    assert 0 <= y0 < y1 <= sheet.height
