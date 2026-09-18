"""Знаки отметок уровня на строительном листе (план, Ф7, E10).

Модель, читающая лист целиком, выдаёт размеры за отметки и путает знак:
E10 на 10 реальных DWG — 22 из 28 при 14 выдуманных. Знак отметки (ГОСТ
21.101) на листе однозначен: вертикальная черта, на одном конце — открытая
стрелка из двух коротких штрихов под 45°, упёртая в линию уровня, на другом —
горизонтальная полка с числом. Найденный знак — место, где число СТОИТ:
модели задаётся узкий вопрос по вырезу вокруг него, и выдумывать становится
не из чего.

Замер (E10, рендер DXF): через знаки 20 из 28 при 2 лишних. Знаки должны
быть не мельче ~12 px: на сборном листе фасадов при 5000 px знак находился
один, при 12 000 px — 11 (фасад 6/8 без лишних). Не находятся отметки
планов — они в прямоугольной рамке, другое обозначение.
"""

from __future__ import annotations

import math
from typing import Any

# Штрих стрелки — под 45° с допуском, короткий.
_DIAGONAL_TOLERANCE_DEG = 12.0
_VERTICAL_TOLERANCE_DEG = 6.0


def level_marks(gray: Any) -> list[tuple[int, int, int]]:
    """Знаки отметок: (x, y) вершины стрелки и длина штриха, px."""
    import cv2
    import numpy as np

    ink = (np.asarray(gray) < 160).astype(np.uint8) * 255
    detector = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD)
    found = detector.detect(255 - ink)[0]
    if found is None:
        return []
    segments = found.reshape(-1, 4)
    lengths = np.hypot(segments[:, 2] - segments[:, 0], segments[:, 3] - segments[:, 1])
    angles = (
        np.degrees(np.arctan2(segments[:, 3] - segments[:, 1], segments[:, 2] - segments[:, 0]))
        + 180.0
    ) % 180.0

    def ends(index: int) -> list[tuple[float, float]]:
        x1, y1, x2, y2 = segments[index]
        return [(float(x1), float(y1)), (float(x2), float(y2))]

    short = [i for i in range(len(segments)) if 4.0 <= lengths[i] <= 80.0]
    rising = [i for i in short if abs(angles[i] - 45.0) < _DIAGONAL_TOLERANCE_DEG]
    falling = [i for i in short if abs(angles[i] - 135.0) < _DIAGONAL_TOLERANCE_DEG]
    vertical = [
        i
        for i in range(len(segments))
        if lengths[i] >= 10.0 and abs(angles[i] - 90.0) < _VERTICAL_TOLERANCE_DEG
    ]
    marks: list[tuple[int, int, int]] = []
    for a in rising:
        for b in falling:
            reach = max(lengths[a], lengths[b])
            if abs(lengths[a] - lengths[b]) > 0.5 * reach:
                continue
            best = None
            for pa in ends(a):
                for pb in ends(b):
                    gap = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                    if gap <= 0.35 * reach + 2.0 and (best is None or gap < best[0]):
                        best = (gap, ((pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0))
            if best is None:
                continue
            vx, vy = best[1]
            tail_a = [p for p in ends(a) if math.hypot(p[0] - vx, p[1] - vy) > 0.5 * lengths[a]]
            tail_b = [p for p in ends(b) if math.hypot(p[0] - vx, p[1] - vy) > 0.5 * lengths[b]]
            # Открытая стрелка: оба штриха уходят от вершины в одну сторону.
            if not tail_a or not tail_b or np.sign(tail_a[0][1] - vy) != np.sign(tail_b[0][1] - vy):
                continue
            # Из вершины стрелки — вертикальная черта к полке.
            if any(
                math.hypot(p[0] - vx, p[1] - vy) <= 0.4 * reach + 3.0
                for v in vertical
                for p in ends(v)
            ):
                mark = (round(vx), round(vy), round(float(lengths[a])))
                if all(math.hypot(mark[0] - m[0], mark[1] - m[1]) > 10 for m in marks):
                    marks.append(mark)
    return marks


LEVEL_AT_MARK_PROMPT = (
    "В центре фрагмента — знак отметки уровня (стрелка, упёртая в линию, и "
    "полка). Какое число стоит на полке ЭТОГО знака? Отметка в метрах с тремя "
    "знаками после точки, знак «+» или «−» как на листе. Если числа у знака "
    'нет — null. ОДНОЙ строкой JSON: {"level": "+3.360"} или {"level": null}. '
    "Только JSON."
)
LEVEL_AT_MARK_SCHEMA = {"type": "object", "properties": {"level": {"type": ["string", "null"]}}}


def mark_crop_box(mark: tuple[int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Вырез вокруг знака: полка с числом уходит вбок от черты — вдвое шире."""
    x, y, stroke = mark
    reach = max(120, 8 * stroke)
    return (
        max(0, x - 2 * reach),
        max(0, y - reach),
        min(size[0], x + 2 * reach),
        min(size[1], y + reach),
    )
