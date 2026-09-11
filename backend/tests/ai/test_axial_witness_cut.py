"""Размерная линия цепочки мерится между выносными, а не всей цепочкой.

Числа — из корпуса с эталоном (эксперимент E2 плана): цифра 31 px, выносная
уходит к детали на сотни px и за линию на 23 px, стрелка длиной 41 px.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.axial_dimensions import _ink_rows, _span_from_ink

ROW = 200
UNIT = 31.0


def _chain(witnesses: list[int], *, outside: set[tuple[int, int]] = frozenset()):
    """Цепочка размеров на одной строке, как её рисует перечерчивание."""
    image = Image.new("L", (800, 700), 255)
    draw = ImageDraw.Draw(image)
    draw.line([(witnesses[0] - 80, ROW), (witnesses[-1] + 80, ROW)], fill=0, width=3)
    for x in witnesses:
        draw.line([(x, ROW - 23), (x, ROW + 400)], fill=0, width=3)
    for left, right in zip(witnesses, witnesses[1:]):
        # Залитая стрелка: остриё у выносной, основание — внутрь размера или,
        # если места нет, снаружи.
        step = -41 if (left, right) in outside else 41
        for tip, back in ((left, left + step), (right, right - step)):
            draw.polygon([(tip, ROW), (back, ROW - 11), (back, ROW + 11)], fill=0)
    return _ink_rows(image)


def _label(left: int, right: int) -> list[float]:
    centre = (left + right) / 2
    return [centre - 13, ROW - 50, centre + 13, ROW - 19]


def test_a_link_of_the_chain_is_cut_at_its_witness_lines_not_at_arrow_backs():
    """Задник залитой стрелки тоже пересекает строку вверх и вниз — на 11 px.

    Первая версия отсечения резала по нему: «15» мерилось как 94 px вместо 177.
    """
    ink = _chain([100, 277, 572, 785 - 100])

    line = _span_from_ink(ink, _label(277, 572), UNIT)

    assert line is not None
    assert abs((line[2] - line[0]) - (572 - 277)) <= 2


def test_a_short_link_with_outside_arrows_is_measured_between_its_witness_lines():
    """6 мм = 71 px: короче порога самого прогона, но выносные подтверждены."""
    ink = _chain([150, 400, 471, 650], outside={(400, 471)})

    line = _span_from_ink(ink, _label(400, 471), UNIT)

    assert line is not None
    assert abs((line[2] - line[0]) - 71) <= 2
    assert np.isclose(line[0], 400, atol=2)
