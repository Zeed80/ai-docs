"""Оси, масштаб и стены плана этажа — замером по листу (Ф7.1, Ф7.2)."""

import cv2
import numpy as np

from app.ai.construction_axes import axis_markers, chain_crop_box, marker_rows, scale_from_spans
from app.ai.construction_walls import wall_pairs


def _plan_sheet() -> np.ndarray:
    """Лист: ряд маркеров осей через весь лист и кучка мелких кружков рядом."""
    sheet = np.full((1300, 2400, 3), 255, np.uint8)
    for x in (300, 1100, 2000):
        cv2.circle(sheet, (x, 1100), 40, (0, 0, 0), 3)
        cv2.putText(sheet, "1", (x - 12, 1118), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 3)
    # Мелкие кружки (отверстия, узлы) — их больше, но они стоят кучкой.
    for index in range(6):
        cv2.circle(sheet, (700 + 40 * index, 400), 12, (0, 0, 0), 2)
        cv2.putText(
            sheet, ".", (500 + 40 * index, 305), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 2
        )
    return cv2.cvtColor(sheet, cv2.COLOR_BGR2GRAY)


def test_axis_markers_are_the_circles_whose_row_runs_across_the_sheet():
    """Мелких кружков на плане больше, чем маркеров (перекрытия: 14 против 7):
    «самый частый радиус» отбрасывал настоящие маркеры все до одного."""
    markers = axis_markers(_plan_sheet())

    assert len(markers) == 3
    assert all(abs(marker[2] - 40) <= 10 for marker in markers)
    assert len(marker_rows(markers)) == 1


def test_the_scale_needs_two_agreeing_links_of_the_chain():
    # Два согласных звена: 6000 мм на 500 px и 8000 на 665 px.
    assert scale_from_spans([(500.0, 6000.0), (665.0, 8000.0)]) == 12.01504
    # Одно звено — догадка; два несогласных — тоже.
    assert scale_from_spans([(500.0, 6000.0)]) is None
    assert scale_from_spans([(500.0, 6000.0), (500.0, 3000.0)]) is None


def test_a_sheet_not_to_scale_gives_no_scale_even_if_two_links_agree():
    """Живой план «на отм. 0.000» (звенья как их прочла модель): два из шести
    случайно сошлись на 14 мм/px — и замер нашёл 247 «стен» на листе не в
    масштабе. Согласная группа обязана быть большинством."""
    spans = [
        (186.0, 6780.0),
        (283.0, 3000.0),
        (281.0, 3000.0),
        (331.0, 4600.0),
        (472.0, 6780.0),
        (887.0, 6000.0),
    ]
    assert scale_from_spans(spans) is None
    # Большинство согласно — масштаб есть, случайное звено не мешает.
    assert scale_from_spans([(500.0, 6000.0), (1000.0, 12000.0), (400.0, 1000.0)]) == 12.0


def test_the_chain_crop_covers_the_span_between_two_markers():
    box = chain_crop_box((300.0, 1100.0, 40.0), (1100.0, 1100.0, 40.0), 0, (2400, 1300))

    assert box[0] < 300 and box[2] > 1100 and box[1] < 1100 < box[3]


def test_a_wall_is_a_pair_of_parallel_lines_and_the_same_wall_is_not_counted_twice():
    lines = [
        ("h", 100.0, 0.0, 900.0),  # грани стены 120 мм при 0,5 мм/px
        ("h", 340.0, 0.0, 900.0),
        ("h", 100.0, 0.0, 880.0),  # та же стена, чуть другой кусок грани
        ("v", 500.0, 0.0, 200.0),  # короткая пара — не стена
        ("v", 560.0, 0.0, 200.0),
    ]
    walls = wall_pairs(lines, min_thickness=180.0, max_thickness=1400.0, min_length=800.0)

    assert len(walls) == 1
    wall = walls[0]
    assert wall.axis == "h" and wall.thickness == 240.0 and wall.position == 220.0


def _read_wall(**fields):
    from app.ai.construction_reader import WallRead

    base = {
        "id": "w1",
        "name": "наружная",
        "start_x_mm": 0.0,
        "start_y_mm": 0.0,
        "end_x_mm": 8000.0,
        "end_y_mm": 0.0,
        "material": "кирпич",
        "load_bearing": True,
    }
    return WallRead(**{**base, **fields})


def test_measured_walls_are_millimetres_from_the_lower_left_axis_marker():
    """Стена на листе — в пикселях; в модель она попадает в координатах
    плана, а рамка находки остаётся пиксельной (вырез листа, Ф9)."""
    from app.ai.construction_walls import WallSegment, walls_as_read

    wall = WallSegment(axis="h", position=800.0, start=100.0, end=1100.0, thickness=25.0)

    read = walls_as_read([wall], mm_per_px=10.0, origin_px=(100.0, 900.0))[0]

    assert (read["start_x_mm"], read["end_x_mm"]) == (0.0, 10000.0)
    # Ось y листа идёт вниз, у здания — вверх.
    assert read["start_y_mm"] == 1000.0
    assert read["thickness_mm"] == 250.0
    assert read["bbox_px"] == [100.0, 787.5, 1100.0, 812.5]


def test_the_sheet_gives_the_thickness_the_model_could_not_read():
    """Живой план: у стен не было ни одной толщины — схема требовала число, и
    стена молча исключалась. Толщина измерима, и надписи остаются от чтения."""
    from app.ai.construction_walls import reconcile_walls

    measured = [
        {
            "id": "wall-1",
            "name": "стена по листу 1",
            "start_x_mm": 0.0,
            "start_y_mm": 0.0,
            "end_x_mm": 8010.0,
            "end_y_mm": 0.0,
            "thickness_mm": 380.0,
            "length_mm": 8010.0,
            "bbox_px": [1.0, 2.0, 3.0, 4.0],
        }
    ]

    walls, verdicts = reconcile_walls([_read_wall(thickness_mm=None)], measured)

    assert walls[0]["thickness_mm"] == 380.0
    assert walls[0]["material"] == "кирпич" and walls[0]["id"] == "w1"
    assert verdicts[0]["status"] == "confirmed"
    assert verdicts[0]["evidence_bbox_px"] == [1.0, 2.0, 3.0, 4.0]


def test_a_wall_the_measurement_missed_is_not_refuted_and_an_extra_one_is_added():
    """Замер находит не всё (на листах — 95 %), поэтому «пары нет» — это «не
    измеримо», а не опровержение; найденная лишняя стена добавляется в модель."""
    from app.ai.construction_walls import reconcile_walls

    other = {
        "id": "wall-2",
        "name": "стена по листу 2",
        "start_x_mm": 0.0,
        "start_y_mm": 0.0,
        "end_x_mm": 0.0,
        "end_y_mm": 5000.0,
        "thickness_mm": 120.0,
        "length_mm": 5000.0,
        "bbox_px": [5.0, 6.0, 7.0, 8.0],
    }

    walls, verdicts = reconcile_walls([_read_wall(thickness_mm=380.0)], [other])

    assert [verdict["status"] for verdict in verdicts] == ["unmeasurable", "confirmed"]
    assert verdicts[0]["kind"] == "construction_wall"
    assert verdicts[1]["kind"] == "construction_wall_found"
    assert verdicts[1]["path"] == "walls[1]"
    assert len(walls) == 2 and walls[1]["thickness_mm"] == 120.0


def test_doors_and_windows_on_a_generated_plan_are_found_with_their_kind():
    """Ф7.3: проём — разрыв стены; дверь узнаётся по дуге открывания, окно —
    по остеклению (тонкая пара линий в разрыве). Эталон — по построению."""
    from app.ai.construction_walls import find_openings, find_walls
    from app.ai.verify_corpus.construction_plan import random_plan

    image, truth = random_plan(3)
    gray = np.asarray(image)
    found = find_openings(gray, find_walls(gray, truth.mm_per_px), truth.mm_per_px)

    expected = sorted((item["axis"], item["kind"], round(item["start"])) for item in truth.openings)
    got = sorted((item["gap"].axis, item["kind"], round(item["gap"].start)) for item in found)
    assert [(axis, kind) for axis, kind, _ in got] == [(axis, kind) for axis, kind, _ in expected]
    assert all(abs(a[2] - b[2]) <= 4 for a, b in zip(got, expected, strict=True))


def test_the_solid_pier_between_two_windows_is_not_an_opening():
    """Остекления двух окон одной стены — «тонкие стены» на одной осевой;
    разрыв между ними — глухой простенок, его закрывает сама стена (22
    лишних «проёма» на корпусе до правила)."""
    from app.ai.construction_walls import WallSegment, wall_gaps

    outer = [
        WallSegment("h", 100.0, 0.0, 200.0, 60.0),
        WallSegment("h", 100.0, 400.0, 700.0, 60.0),
        WallSegment("h", 100.0, 900.0, 1100.0, 60.0),
    ]
    glazing = [
        WallSegment("h", 100.0, 200.0, 400.0, 20.0),
        WallSegment("h", 100.0, 700.0, 900.0, 20.0),
    ]

    gaps = wall_gaps(outer + glazing, min_gap=60.0, max_gap=400.0)

    assert sorted((gap.start, gap.end) for gap in gaps) == [(200.0, 400.0), (700.0, 900.0)]


def test_a_plan_wall_drawn_on_its_boundary_keeps_its_thickness():
    """Генератор рисовал контур внутрь стены: перегородка 120 мм мерилась как
    88 — ниже порога стены, и её двери пропадали (5 из 67)."""
    from app.ai.construction_walls import find_walls
    from app.ai.verify_corpus.construction_plan import random_plan

    image, truth = random_plan(7)
    walls = find_walls(np.asarray(image), truth.mm_per_px)

    assert any(
        wall.axis == "v" and abs(wall.position - 975) <= 8 and 100 <= wall.thickness * 8 <= 140
        for wall in walls
    )


def test_the_majority_counts_the_links_that_could_not_be_read():
    """Тот же скан не в масштабе, другие маркеры: прочитались лишь два звена
    слева (оба 3000) из семи — «два из двух» приняли масштаб."""
    spans = [(283.2, 3000.0), (281.3, 3000.0)]

    assert scale_from_spans(spans) == 10.629
    assert scale_from_spans(spans, attempted=7) is None
    assert scale_from_spans(spans, attempted=3) == 10.629


def test_axis_markers_are_found_on_a_plan_with_hatched_walls():
    """Хаф по плану со штриховкой стен: 33 маркера из 145 при 342 лишних.
    Маркер — кольцо с круглой дырой, штриховка круглых дыр не даёт."""
    from app.ai.verify_corpus.construction_plan import random_plan

    image, truth = random_plan(3)
    markers = axis_markers(np.asarray(image))

    assert len(markers) == len(truth.markers)
    for x, y, radius in truth.markers:
        assert any(abs(m[0] - x) <= 3 and abs(m[1] - y) <= 3 for m in markers)
        assert any(abs(m[2] - radius) <= 3 for m in markers)
