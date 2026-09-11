"""Размерная линия цепочки мерится между выносными, а не всей цепочкой.

Числа — из корпуса с эталоном (эксперимент E2 плана): цифра 31 px, выносная
уходит к детали на сотни px и за линию на 23 px, стрелка длиной 41 px.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app.ai.cad_recognize.axial_dimensions import _ink_rows, _span_from_ink

ROW = 200
UNIT = 31.0


def _chain(
    witnesses: list[int],
    *,
    outside: set[tuple[int, int]] = frozenset(),
    paper: int = 255,
    ink: int = 0,
    label_at: tuple[int, int, int, int] | None = None,
    blur: float = 0.0,
    arrow: tuple[int, int] = (41, 11),
    extension: int = 23,
    width: int = 3,
):
    """Цепочка размеров на одной строке, как её рисует перечерчивание."""
    image = Image.new("L", (800, 700), paper)
    draw = ImageDraw.Draw(image)
    draw.line([(witnesses[0] - 80, ROW), (witnesses[-1] + 80, ROW)], fill=ink, width=width)
    for x in witnesses:
        draw.line([(x, ROW - extension), (x, ROW + 400)], fill=ink, width=width)
    for left, right in zip(witnesses, witnesses[1:]):
        # Залитая стрелка: остриё у выносной, основание — внутрь размера или,
        # если места нет, снаружи.
        length, wing = arrow
        step = -length if (left, right) in outside else length
        for tip, back in ((left, left + step), (right, right - step)):
            draw.polygon([(tip, ROW), (back, ROW - wing), (back, ROW + wing)], fill=ink)
    if label_at is not None:
        # Цифра «8» — два кольца, низ — на `label_at[2]` px над линией.
        x0, x1, gap, height = label_at
        middle = ROW - gap - height // 2
        draw.ellipse([x0, ROW - gap - height, x1, middle], outline=ink, width=2)
        draw.ellipse([x0, middle, x1, ROW - gap], outline=ink, width=2)
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
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


def test_a_faint_line_on_tinted_paper_is_still_ink():
    """Фото: бумага ~228, тонкие линии после размытия ~190.

    Абсолютный порог 160 оставлял от размера одни стрелки — на фото
    находилась треть размеров.
    """
    ink = _chain([150, 400, 700], paper=228, ink=190)

    line = _span_from_ink(ink, _label(150, 400), UNIT)

    assert line is not None
    assert abs((line[2] - line[0]) - 250) <= 2


def test_a_blurred_label_fused_with_the_arrows_is_not_a_witness_line():
    """Размытие: низ цифры сливается со стрелками под ней.

    Геометрия — как на ступени blur-150 корпуса: размер 8 мм = 47 px, стрелки
    внутри встречаются основаниями под цифрой высотой 16 px. Столбцы под
    подписью получали длинную сторону вверх — через саму цифру, — и штрихи «8»
    резали размер 47 px до 5. Подпись вырезается из подсчёта.
    """
    left, right = 300, 347
    ink = _chain(
        [120, left, right, 560],
        label_at=(317, 330, 9, 16),
        blur=1.2,
        arrow=(21, 6),
        extension=12,
        width=2,
    )

    line = _span_from_ink(ink, [317.0, ROW - 25.0, 330.0, ROW - 9.0], 16.0)

    assert line is not None
    assert abs((line[2] - line[0]) - (right - left)) <= 2
