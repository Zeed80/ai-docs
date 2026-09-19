"""Знаки отметок уровня (Ф7, E10): найти знак — прочитать число только у него."""

from __future__ import annotations

import cv2
import numpy as np

from app.ai.construction_levels import level_boxes, level_marks, mark_crop_box


def _sheet(with_mark: bool = True) -> np.ndarray:
    sheet = np.full((600, 900), 255, np.uint8)
    cv2.line(sheet, (100, 400), (800, 400), 0, 2)  # линия уровня
    cv2.line(sheet, (150, 200), (850, 200), 0, 2)  # размерная — не отметка
    cv2.putText(sheet, "3000", (450, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2)
    if with_mark:
        # Стрелка вершиной на линии уровня, черта вверх, полка вправо.
        cv2.line(sheet, (400, 400), (380, 380), 0, 2)
        cv2.line(sheet, (400, 400), (420, 380), 0, 2)
        cv2.line(sheet, (400, 400), (400, 320), 0, 2)
        cv2.line(sheet, (400, 320), (520, 320), 0, 2)
        cv2.putText(sheet, "+3.360", (405, 312), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
    return sheet


def test_a_level_mark_is_found_at_its_arrow_and_nothing_else_is():
    marks = level_marks(_sheet())

    assert len(marks) == 1
    x, y, _stroke, side = marks[0]
    assert abs(x - 400) <= 8 and abs(y - 400) <= 8
    assert side == -1  # полка над вершиной


def test_a_sheet_without_level_marks_gives_nothing_to_read():
    """План без отметок: чтение целым листом выдумывало их, знаков — ноль."""
    assert level_marks(_sheet(with_mark=False)) == []


def test_the_crop_holds_the_shelf_beside_the_mark():
    box = mark_crop_box((400, 400, 28), (900, 600))
    assert box[0] < 400 < box[2] and box[1] < 320 and box[2] > 520


def test_the_crop_of_a_stacked_mark_stays_on_its_own_shelf_side():
    """Фасад 1-3: знаки стопкой через ~70 px — вырез не заходит к соседнему."""
    box = mark_crop_box((400, 400, 14, -1), (900, 900))
    assert box[1] <= 400 - 120  # своя полка и число над ней — внутри
    assert box[3] < 400 + 33  # полка знака ниже (на 69 − 36 px) — нет


def _boxed(*, crossed: bool = False, broken_corner: bool = False) -> np.ndarray:
    sheet = np.full((600, 900), 255, np.uint8)
    x0, y0, x1, y1 = 300, 250, 640, 375
    cv2.line(sheet, (x0, y0), (x1, y0), 0, 1)
    cv2.line(sheet, (x1, y0), (x1, y1), 0, 1)
    # Разорванный угол, как у «−1.800» на плане: левая сторона не доходит
    # до нижней, нижняя начинается правее.
    cv2.line(sheet, (x0, y0), (x0, y1 - (8 if broken_corner else 0)), 0, 1)
    cv2.line(sheet, (x0 + (14 if broken_corner else 0), y1), (x1, y1), 0, 1)
    cv2.putText(sheet, "-1.800", (340, 340), cv2.FONT_HERSHEY_SIMPLEX, 2.2, 0, 5)
    if crossed:
        cv2.line(sheet, (330, 210), (420, 420), 0, 1)
    return sheet


def test_a_plan_level_in_a_frame_is_found_even_crossed_and_with_a_broken_corner():
    for sheet in (_boxed(), _boxed(crossed=True, broken_corner=True)):
        boxes = level_boxes(sheet)
        assert len(boxes) == 1
        x0, y0, x1, y1 = boxes[0]
        assert abs(x0 - 300) <= 3 and abs(y0 - 250) <= 3
        assert abs(x1 - 640) <= 3 and abs(y1 - 375) <= 3


def test_a_thick_arc_is_not_a_level_frame():
    sheet = np.full((600, 900), 255, np.uint8)
    cv2.ellipse(sheet, (450, 100), (400, 200), 0, 30, 150, 0, 2)
    assert level_boxes(sheet) == []


def test_the_arrow_of_a_long_vertical_dimension_is_not_a_level_mark():
    """Стрелка вертикальной размерной — те же два штриха и вертикаль, но
    черта тянется через весь размер («3,950» на фасаде шло отметкой)."""
    sheet = np.full((900, 900), 255, np.uint8)
    cv2.line(sheet, (400, 100), (400, 800), 0, 2)
    cv2.line(sheet, (400, 800), (380, 780), 0, 2)
    cv2.line(sheet, (400, 800), (420, 780), 0, 2)
    cv2.line(sheet, (300, 800), (500, 800), 0, 2)
    assert level_marks(sheet) == []


def test_the_sheet_levels_come_only_from_found_marks_in_level_format():
    import asyncio
    import io

    from PIL import Image

    from app.ai.construction_levels import normalize_level, read_sheet_levels

    # Лист в размер, при котором знаки ищутся без увеличения (5000 px).
    sheet = np.full((3000, 5000), 255, np.uint8)
    sheet[1000:1600, 2000:2900] = _boxed()
    buffer = io.BytesIO()
    Image.fromarray(sheet).save(buffer, format="PNG")
    asked: list[str] = []

    async def ask(prompt, image):
        asked.append(prompt)
        return {"level": "−1,800"}

    result = asyncio.run(read_sheet_levels(buffer.getvalue(), ask=ask))
    # Одна рамка — один вопрос; значение приведено к формату отметки.
    assert len(asked) == 1
    assert result["values"] == ["-1.800"]
    x0, y0, x1, y1 = result["levels"][0]["bbox_px"]
    assert x0 < 2300 < 2640 < x1 and y0 < 1250 < 1375 < y1

    async def dimension(prompt, image):
        return {"level": "3000"}

    assert asyncio.run(read_sheet_levels(buffer.getvalue(), ask=dimension))["values"] == []
    assert normalize_level("+0,000") == "0.000"
    assert normalize_level("3 950") is None
