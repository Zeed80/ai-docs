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
