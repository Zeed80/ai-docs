"""Знаки отметок уровня (Ф7, E10): найти знак — прочитать число только у него."""

from __future__ import annotations

import cv2
import numpy as np

from app.ai.construction_levels import level_marks, mark_crop_box


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
    x, y, _stroke = marks[0]
    assert abs(x - 400) <= 8 and abs(y - 400) <= 8


def test_a_sheet_without_level_marks_gives_nothing_to_read():
    """План без отметок: чтение целым листом выдумывало их, знаков — ноль."""
    assert level_marks(_sheet(with_mark=False)) == []


def test_the_crop_holds_the_shelf_beside_the_mark():
    box = mark_crop_box((400, 400, 28), (900, 600))
    assert box[0] < 400 < box[2] and box[1] < 320 and box[2] > 520
