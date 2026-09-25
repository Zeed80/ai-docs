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


# ── Подпись вдоль своей размерной линии ─────────────────────────────────────


def _label_of(kind: str, anchors: list[list[float]]):
    from app.ai.cad_ir.schema import TextEntity

    entities = dimensions_from_kernel(
        [{"view_index": 0, "kind": kind, "label": "", "anchors_mm": anchors, "value_mm": 25.0}],
        {"front": {"offset_u": 50.0, "offset_v": 100.0}},
        ["front"],
        px_per_mm=1.0,
    )
    return next(item for item in entities if isinstance(item, TextEntity))


def test_a_vertical_dimension_reads_bottom_to_top_left_of_its_line():
    """Текст вертикальных размеров не поворачивался и лежал поперёк линии."""
    label = _label_of("DistanceY", [[10.0, -12.5], [10.0, 12.5]])

    assert label.rotation == -90.0
    assert label.anchor == "middle"
    assert label.position.x < 50.0 + 10.0  # слева от линии


def test_a_horizontal_dimension_label_is_centred_not_rotated():
    """Подпись ставится центром; прочитанная как начало, она уезжала вправо."""
    label = _label_of("DistanceX", [[0.0, 0.0], [25.0, 0.0]])

    assert label.rotation == 0.0
    assert label.anchor == "middle"


def test_a_shaft_diameter_is_drawn_on_its_own_step_without_witness_lines():
    """Ø25 на ступени длиной 12 уезжал на соседнюю: к нему применялся отступ длин."""
    entities = dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "kind": "DistanceY",
                "label": "Ø",
                "anchors_mm": [[0.0, -12.5], [0.0, 12.5]],
                "value_mm": 25.0,
                "place_u": 6.0,
            }
        ],
        {"front": {"offset_u": 0.0, "offset_v": 100.0}},
        ["front"],
        px_per_mm=1.0,
    )
    segments = [item for item in entities if isinstance(item, Segment)]

    assert len(segments) == 1  # только размерная линия, без выносных
    assert segments[0].p1.x == segments[0].p2.x == 6.0


def test_a_plate_height_still_goes_outside_with_witness_lines():
    """Высота пластины — тоже DistanceY, но без `place_u`: выносится за контур."""
    entities = dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "kind": "DistanceY",
                "label": "",
                "anchors_mm": [[0.0, -25.0], [0.0, 25.0]],
                "value_mm": 50.0,
            }
        ],
        {"front": {"offset_u": 50.0, "offset_v": 100.0}},
        ["front"],
        px_per_mm=1.0,
    )
    segments = [item for item in entities if isinstance(item, Segment)]

    assert len(segments) == 3  # две выносные и размерная
    assert segments[2].p1.x < 50.0  # в стороне от контура


def test_hole_coordinates_left_of_a_plan_stack_in_rows():
    """Все координаты по v начинаются от нижней кромки — без рядов легли бы на одну линию."""
    dimensions = [
        {
            "view_index": 0,
            "kind": "DistanceY",
            "outside": True,
            "place_u": -50.0,
            "anchors_mm": [[-50.0, -25.0], [10.0, 9.0]],
            "value_mm": 34.0,
            "label": "34",
        },
        {
            "view_index": 0,
            "kind": "DistanceY",
            "outside": True,
            "place_u": -50.0,
            "anchors_mm": [[-50.0, -25.0], [-28.0, 14.0]],
            "value_mm": 39.0,
            "label": "39",
        },
    ]
    tiers = _length_tiers(dimensions)
    assert tiers == {0: 0, 1: 1}

    entities = dimensions_from_kernel(
        dimensions, {"side": {"offset_u": 100.0, "offset_v": 100.0}}, ["side"], px_per_mm=1.0
    )
    lines = [s for s in entities if isinstance(s, Segment) and s.p1.x == s.p2.x]
    xs = sorted({round(s.p1.x, 3) for s in lines})
    assert len(xs) == 2 and xs[1] - xs[0] == 7.0  # два ряда, шаг 7 мм
    assert all(x < 100.0 - 50.0 for x in xs)  # слева от плана


def test_a_pitch_circle_is_drawn_as_a_thin_centre_line():
    from app.ai.cad_ir.schema import Circle

    entities = dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "kind": "Diameter",
                "pitch_circle": True,
                "label": "Ø73",
                "anchors_mm": [[-36.5, 0.0], [36.5, 0.0]],
                "value_mm": 73.0,
            }
        ],
        {"side": {"offset_u": 100.0, "offset_v": 100.0}},
        ["side"],
        px_per_mm=1.0,
    )
    circles = [e for e in entities if isinstance(e, Circle)]
    assert len(circles) == 1
    assert circles[0].line_class == "axis" and circles[0].radius == 36.5


def _diameter_label(radius: float, label: str):
    from app.ai.cad_ir.schema import TextEntity

    entities = dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "kind": "Diameter",
                "label": label,
                "anchors_mm": [[-radius, 0.0], [radius, 0.0]],
                "value_mm": 2 * radius,
            }
        ],
        {"side": {"offset_u": 100.0, "offset_v": 100.0}},
        ["side"],
        px_per_mm=1.0,
    )
    text = next(item for item in entities if isinstance(item, TextEntity))
    segments = [item for item in entities if isinstance(item, Segment)]
    return text, segments


def test_the_label_of_a_small_hole_goes_past_the_circle_onto_a_shelf():
    """«4 отв. Ø5.5» ложился поперёк окружности Ø5.5 поверх обеих стрелок."""
    text, segments = _diameter_label(2.75, "4 отв. Ø5.5")

    assert text.position.x - 100.0 > 2.75 + 3.5  # за окружностью и стрелкой
    assert len(segments) == 2  # размерная линия и полка под подписью


def test_the_label_of_a_large_circle_stays_on_its_diameter():
    text, segments = _diameter_label(60.0, "Ø120")

    assert abs(text.position.x - 100.0) < 60.0
    assert len(segments) == 1


def test_a_radius_runs_from_the_centre_with_one_arrow_on_the_arc():
    """Радиус рисовался как длина: отступ и две выносные в стороне от скругления."""
    from app.ai.cad_ir.schema import Polyline

    entities = dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "kind": "Radius",
                "label": "R10",
                "anchors_mm": [[0.0, 0.0], [7.0711, 7.0711]],
                "value_mm": 10.0,
            }
        ],
        {"side": {"offset_u": 100.0, "offset_v": 100.0}},
        ["side"],
        px_per_mm=1.0,
    )
    arrows = [e for e in entities if isinstance(e, Polyline)]
    segments = [e for e in entities if isinstance(e, Segment)]

    assert len(arrows) == 1
    assert (segments[0].p1.x, segments[0].p1.y) == (100.0, 100.0)  # от самого центра
    assert abs(arrows[0].points[0].x - 107.0711) < 1e-3  # остриё — на дуге


def test_a_diameter_label_crossing_its_own_circle_goes_onto_the_shelf():
    """Базовая линия v3, plate-0: «Ø11» лежал на контуре своего отверстия Ø11 —
    ридер прочитал отверстие как Ø15."""
    text, segments = _diameter_label(5.5, "Ø11")

    assert text.position.x - 100.0 > 5.5  # за окружностью
    assert len(segments) == 2


def test_a_bore_label_that_fits_inside_stays_on_its_diameter():
    text, segments = _diameter_label(16.0, "Ø32")

    assert abs(text.position.x - 100.0) < 16.0
    assert len(segments) == 1


def test_a_feature_dimension_marked_below_stands_under_the_view():
    """Размеры паза и отверстий вала — под видом, чтобы не делить ряды с цепочкой."""
    from app.ai.cad_ir.schema import TextEntity

    entities = dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "kind": "DistanceX",
                "below": True,
                "label": "19",
                "anchors_mm": [[-117.2, -4.0], [-98.2, -4.0]],
                "value_mm": 19.0,
            }
        ],
        {
            "bottom": {
                "offset_u": 200.0,
                "offset_v": 100.0,
                "bounds_mm": {"v_min": -20.0, "v_max": 20.0},
            }
        },
        ["bottom"],
        px_per_mm=1.0,
    )
    segments = [e for e in entities if isinstance(e, Segment)]
    line = segments[2]
    label = next(e for e in entities if isinstance(e, TextEntity))

    assert line.p1.y == line.p2.y > 120.0  # ниже нижней кромки вида (y вниз)
    assert label.position.y < line.p1.y  # подпись — над своей линией


def test_short_feature_dimensions_whose_labels_would_touch_go_to_separate_rows():
    """shaft-1 (1:2): «9.8» и «16.7» в одном ряду под видом слиплись в «9.816.7».

    На листе 1:2 положения 9,8 и 16,7 — это 4,9 и 8,35 мм листа, а зазор между
    ними — 2,6 мм: подписи шире своих линий налезают друг на друга.
    """
    dimensions = [
        {
            "view_index": 0,
            "kind": "DistanceX",
            "below": True,
            "label": "9.8",
            "anchors_mm": [[0.0, 0.0], [4.9, 0.0]],
            "value_mm": 9.8,
        },
        {
            "view_index": 0,
            "kind": "DistanceX",
            "below": True,
            "label": "16.7",
            "anchors_mm": [[7.5, 0.0], [15.85, 0.0]],
            "value_mm": 16.7,
        },
    ]
    tiers = _length_tiers(dimensions)
    assert tiers[0] != tiers[1]


def _chain_labels(links: list[tuple[float, float, str]]):
    from app.ai.cad_ir.schema import TextEntity

    entities = dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "kind": "DistanceX",
                "label": label,
                "anchors_mm": [[left, 0.0], [right, 0.0]],
                "value_mm": right - left,
            }
            for left, right, label in links
        ],
        {"front": {"offset_u": 100.0, "offset_v": 100.0, "bounds_mm": {"v_max": 10.0}}},
        ["front"],
        px_per_mm=1.0,
    )
    return {item.text: item.position.x - 100.0 for item in entities if isinstance(item, TextEntity)}


def test_a_short_link_between_neighbours_keeps_its_label_over_itself():
    """Полка «15» короткого звена ложилась на строку соседнего и читалась его
    подписью (корпус v10: 44 px мерились как 236)."""
    labels = _chain_labels([(0.0, 40.0, "40"), (40.0, 43.0, "15.5"), (43.0, 90.0, "47")])

    assert 40.0 <= labels["15.5"] <= 43.0


def test_a_short_link_at_the_end_of_the_chain_still_takes_the_free_side():
    labels = _chain_labels([(0.0, 40.0, "40"), (40.0, 43.0, "15.5")])

    assert labels["15.5"] > 43.0


def test_rows_under_a_view_with_cutting_plane_traces_stand_below_the_traces():
    """Подписи первого ряда под видом стояли в полосе следов секущих: «17» и
    «19» на стрелках следов Б и В (shaft-29)."""
    from app.ai.cad_ir.schema import TextEntity

    def label_y(reserve: float) -> float:
        bounds = {"v_min": -10.0, "v_max": 10.0, "u_min": 0.0, "u_max": 100.0}
        if reserve:
            bounds["below_reserve_mm"] = reserve
        entities = dimensions_from_kernel(
            [
                {
                    "view_index": 0,
                    "kind": "DistanceX",
                    "label": "17",
                    "below": True,
                    "anchors_mm": [[20.0, -10.0], [37.0, -10.0]],
                    "value_mm": 17.0,
                }
            ],
            {"front": {"offset_u": 100.0, "offset_v": 100.0, "bounds_mm": bounds}},
            ["front"],
            px_per_mm=1.0,
        )
        return next(e for e in entities if isinstance(e, TextEntity)).position.y

    # Ниже на ширину полосы следа (y листа растёт вниз).
    assert abs(label_y(6.5) - label_y(0.0) - 6.5) < 1e-6


def test_a_hole_label_does_not_land_on_a_neighbouring_hole():
    """plate-1 корпуса: «Ø11 гл.15» стояла поверх Ø6.6, «M5 гл.18» — поверх
    Ø11, и ридер приписал глубины не тем отверстиям. Полка подписи диаметра
    уходит туда, где на ней нет чужой окружности."""
    from app.ai.cad_ir.schema import TextEntity
    from app.ai.cad_projection import dimensions_from_kernel

    dimensions = [
        {
            "kind": "Diameter",
            "view_index": 0,
            "value_mm": 4.0,
            "label": "Ø4 гл.15",
            "anchors_mm": [[-1.414, -1.414], [1.414, 1.414]],
        },
        {
            "kind": "Diameter",
            "view_index": 0,
            "value_mm": 10.0,
            "anchors_mm": [[5.0 - 3.536, 12.0 - 3.536], [5.0 + 3.536, 12.0 + 3.536]],
        },
    ]
    placements = {
        "top": {
            "offset_u": 100.0,
            "offset_v": 100.0,
            "bounds_mm": {"u_min": -40.0, "u_max": 40.0, "v_min": -40.0, "v_max": 40.0},
        }
    }

    entities = dimensions_from_kernel(dimensions, placements, ["top"], px_per_mm=1.0)

    label = next(e for e in entities if isinstance(e, TextEntity) and e.text == "Ø4 гл.15")
    # Вперёд (вверх-вправо) — окружность Ø10: подпись ушла вниз-влево.
    assert label.position.x < 100.0 and label.position.y > 100.0
