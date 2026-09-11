"""Размер рисуется по тому типу, который у ядра запрошен."""

from __future__ import annotations

from app.ai.cad_ir.schema import Segment
from app.ai.cad_projection import (
    _length_tiers,
    _projected_dimension_points,
    dimensions_from_kernel,
)


def test_a_horizontal_distance_between_faces_at_different_heights_stays_horizontal():
    """Живой синтетический вал: габарит «103» лёг ПО ДИАГОНАЛИ через деталь.

    Точки привязки DistanceX между торцами ступенчатого вала лежат на разной
    высоте — одна на верху толстой ступени, другая на тонком конце. Линия,
    проведённая прямо между ними, пересекала деталь наискось.
    """
    (a1, b1), (a2, b2) = _projected_dimension_points("DistanceX", (0.0, 14.0), (103.0, 10.0))

    assert b1[1] == b2[1] == 14.0  # без границ вида — у верхней привязки
    assert (a1, a2) == ((0.0, 14.0), (103.0, 10.0))  # выносные — от настоящих точек


def test_a_length_is_placed_outside_the_view_not_inside_the_part():
    """Привязки TechDraw лежат на торцах, а не на верху контура.

    Выровняв линию по верхней привязке, я получил габарит ВНУТРИ детали, через
    паз. Длина выносится за контур вида — над его верхней границей.
    """
    (_a1, b1), (_a2, b2) = _projected_dimension_points(
        "DistanceX", (0.0, 3.0), (103.0, -2.0), top=14.0
    )
    assert b1[1] == b2[1] == 14.0


def test_overlapping_lengths_go_to_separate_rows_and_touching_ones_share_a_row():
    """Цепочка — в один ряд, габарит поверх неё — вторым рядом."""
    dimensions = [
        {"view_index": 0, "kind": "DistanceX", "anchors_mm": [[0, 0], [70, 0]]},
        {"view_index": 0, "kind": "DistanceX", "anchors_mm": [[70, 0], [88, 0]]},
        {"view_index": 0, "kind": "DistanceX", "anchors_mm": [[0, 0], [103, 0]]},
    ]
    tiers = _length_tiers(dimensions)

    assert tiers[0] == tiers[1] == 0  # звенья цепочки касаются — один ряд
    assert tiers[2] == 1  # габарит перекрывает цепочку — выше


def test_the_order_of_anchors_does_not_flip_the_dimension_into_the_part():
    """Направление «от детали» задаётся порядком — иначе линия уйдёт внутрь."""
    forward = _projected_dimension_points("DistanceX", (0.0, 14.0), (103.0, 10.0))
    backward = _projected_dimension_points("DistanceX", (103.0, 10.0), (0.0, 14.0))

    assert forward == backward


def test_a_diameter_is_drawn_across_its_own_step():
    """Моя первая попытка ставила его левее левой привязки — на СОСЕДНЮЮ ступень.

    Ø25 уехал на ступень Ø28, и подписи легли одна на другую.
    """
    (_a1, b1), (_a2, b2) = _projected_dimension_points("DistanceY", (5.0, 0.0), (3.0, 30.0))
    assert b1[0] == b2[0] == 4.0


def test_a_point_to_point_distance_is_left_alone():
    (a1, b1), (a2, b2) = _projected_dimension_points("Distance", (0.0, 0.0), (3.0, 4.0))
    assert (a1, a2) == (b1, b2) == ((0.0, 0.0), (3.0, 4.0))


def test_the_drawn_dimension_line_is_horizontal():
    entities = dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "kind": "DistanceX",
                "label": "",
                "anchors_mm": [[0.0, 14.0], [103.0, 10.0]],
                "value_mm": 103.0,
            }
        ],
        {"front": {"offset_u": 10.0, "offset_v": 100.0}},
        ["front"],
        px_per_mm=4.0,
    )
    segments = [item for item in entities if isinstance(item, Segment)]
    dimension_line = segments[2]  # две выносные, затем размерная

    assert dimension_line.p1.y == dimension_line.p2.y


def _drawn(length_mm: float) -> tuple[Segment, list]:
    from app.ai.cad_ir.schema import Polyline

    entities = dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "kind": "DistanceX",
                "label": "",
                "anchors_mm": [[0.0, 0.0], [length_mm, 0.0]],
                "value_mm": length_mm,
            }
        ],
        {"front": {"offset_u": 0.0, "offset_v": 100.0}},
        ["front"],
        px_per_mm=1.0,
    )
    segments = [item for item in entities if isinstance(item, Segment)]
    arrows = [item for item in entities if isinstance(item, Polyline)]
    return segments[2], arrows


def test_arrows_that_do_not_fit_stand_outside_the_witness_lines():
    """Размер 6 мм рисовался двумя стрелками, слитыми в «бантик», без линии.

    По ГОСТ 2.307 стрелки, которым не хватает места, ставятся снаружи, а
    размерная линия продлевается за выносные.
    """
    line, arrows = _drawn(6.0)

    assert line.p1.x < 0.0 and line.p2.x > 6.0  # линия продлена за выносные
    left_back = [point.x for point in arrows[0].points[1:]]
    right_back = [point.x for point in arrows[1].points[1:]]
    assert arrows[0].points[0].x == 0.0 and all(x < 0.0 for x in left_back)
    assert arrows[1].points[0].x == 6.0 and all(x > 6.0 for x in right_back)


def test_arrows_that_fit_stay_inside():
    line, arrows = _drawn(30.0)

    assert (line.p1.x, line.p2.x) == (0.0, 30.0)
    assert all(0.0 <= point.x <= 30.0 for arrow in arrows for point in arrow.points)


# ── Дуга: направление не теряется ───────────────────────────────────────────


def test_a_quarter_arc_through_zero_is_a_quarter_not_three_quarters():
    """Правый конец прорези пластины рисовался дугой на 270°.

    Ядро отдаёт полукруг двумя четвертями; вторая идёт от 0° к −90°. Перевод
    приводил углы к 0…360 и сортировал — выходила дуга 0→270.
    """
    from app.ai.cad_projection import _arc_image_angles

    # Центр (12.5, -15), от (15.5, -15) к (12.5, -12): v вверх → в кадре IR
    # (y вниз) это 0° и 270°, и настоящая дуга — четверть 270→360.
    start, end = _arc_image_angles((12.5, -15.0), (15.5, -15.0), (12.5, -12.0))

    assert end - start == 90.0
    assert (start, end) == (270.0, 360.0)


def test_the_midpoint_decides_for_an_arc_longer_than_a_half_circle():
    from app.ai.cad_projection import _arc_image_angles

    # Три четверти окружности: концы те же, середина — с противоположной стороны.
    start, end = _arc_image_angles((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), mid=(-0.7071, -0.7071))

    assert round(end - start) == 270


def test_without_a_midpoint_the_minor_arc_is_taken():
    from app.ai.cad_projection import _arc_image_angles

    start, end = _arc_image_angles((0.0, 0.0), (0.0, 1.0), (1.0, 0.0))
    assert round(end - start) == 90
