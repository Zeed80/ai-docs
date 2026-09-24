"""Стены на плане этажа — замером по листу (план, Ф7.2).

Ридер плана просит у модели координаты стен в миллиметрах, и живой лист «План
на отм. 0.000» дал 4 стены из десятков, ни одной с толщиной. Но стена на плане
— это пара параллельных линий: её осевая, длина и толщина измеримы ровно так
же, как размерная линия у подписи. Читать у модели остаётся то, что написано
словами (материал, «кирпич 380»), а не то, что нарисовано.

Правило одно и для эталона, и для замера: две параллельные линии одного
направления, идущие рядом на расстоянии толщины стены и перекрывающиеся вдоль
себя. Эталон берёт эти пары из точной геометрии DXF, замер — из растра
(`find_walls`), и харнесс сравнивает одно с другим.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Толщина стены на плане этажа: перегородка в полкирпича — 120 мм, наружная
# кирпичная — до 640 мм. Ниже — линии штриховки и выноски, выше — уже не стена,
# а две разные стены рядом.
MIN_THICKNESS_MM = 90.0
MAX_THICKNESS_MM = 700.0
# Короткий кусок стены на плане этажа — это уже простенок между проёмами;
# короче полуметра пара параллельных линий чаще оказывается размерной цепочкой.
MIN_LENGTH_MM = 500.0
# Насколько линии должны перекрываться вдоль себя, чтобы быть гранями одной стены.
_OVERLAP_SHARE = 0.5


@dataclass(frozen=True)
class WallSegment:
    """Стена как её видно на плане: осевая (вдоль оси), толщина, длина.

    ``axis`` — «h» (вдоль x) или «v» (вдоль y); ``position`` — координата
    осевой поперёк стены; ``start``/``end`` — её концы вдоль стены. Единицы —
    те же, в которых пришли линии (пиксели растра или единицы чертежа).
    """

    axis: str
    position: float
    start: float
    end: float
    thickness: float

    @property
    def length(self) -> float:
        return self.end - self.start


def wall_pairs(
    lines: list[tuple[str, float, float, float]],
    *,
    min_thickness: float,
    max_thickness: float,
    min_length: float,
) -> list[WallSegment]:
    """Пары параллельных линий → стены. ``lines``: (ось, положение, от, до).

    Одна линия может войти в несколько пар (стена, к которой примыкает
    перегородка), поэтому пары не «съедают» линии; зато совпадающие стены
    (одна и та же пара, найденная дважды) сливаются.
    """
    found: list[WallSegment] = []
    for axis in ("h", "v"):
        same = [line for line in lines if line[0] == axis]
        for index, (_a, position_a, start_a, end_a) in enumerate(same):
            for _b, position_b, start_b, end_b in same[index + 1 :]:
                thickness = abs(position_a - position_b)
                if not min_thickness <= thickness <= max_thickness:
                    continue
                start, end = max(start_a, start_b), min(end_a, end_b)
                overlap = end - start
                shorter = min(end_a - start_a, end_b - start_b)
                if overlap < min_length or overlap < _OVERLAP_SHARE * shorter:
                    continue
                found.append(
                    WallSegment(
                        axis=axis,
                        position=(position_a + position_b) / 2.0,
                        start=start,
                        end=end,
                        thickness=thickness,
                    )
                )
    return _merge(found)


def _merge(walls: list[WallSegment]) -> list[WallSegment]:
    """Слить стены, которые на листе — одна и та же (те же грани, тот же пролёт)."""
    merged: list[WallSegment] = []
    for wall in sorted(walls, key=lambda item: -(item.end - item.start)):
        twin = next(
            (
                kept
                for kept in merged
                if kept.axis == wall.axis
                and abs(kept.position - wall.position) <= 0.5 * kept.thickness
                and abs(kept.thickness - wall.thickness) <= 0.35 * kept.thickness
                and min(kept.end, wall.end) - max(kept.start, wall.start)
                >= 0.5 * (wall.end - wall.start)
            ),
            None,
        )
        if twin is None:
            merged.append(wall)
    return merged


def sheet_lines(gray: Any, *, min_length_px: float) -> list[tuple[str, float, float, float]]:
    """Прямые линии листа вдоль осей: (ось, положение, от, до), px."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _lines

    ink = _ink(np.asarray(gray))
    result: list[tuple[str, float, float, float]] = []
    for axis, name in ((0, "h"), (1, "v")):
        for line in _lines(ink, max(4, int(round(min_length_px))), axis=axis):
            result.append((name, float(line.position), float(line.start), float(line.end)))
    return result


def find_walls(gray: Any, mm_per_px: float) -> list[WallSegment]:
    """Стены плана по растру: пары граней в миллиметрах чертежа.

    ``mm_per_px`` — масштаб плана (Ф7.1): без него «толщина 27 px» ничего не
    значит — это и перегородка при одном масштабе, и наружная стена при другом.
    """
    if not mm_per_px or mm_per_px <= 0:
        return []
    lines = sheet_lines(gray, min_length_px=MIN_LENGTH_MM / mm_per_px)
    return wall_pairs(
        lines,
        min_thickness=MIN_THICKNESS_MM / mm_per_px,
        max_thickness=MAX_THICKNESS_MM / mm_per_px,
        min_length=MIN_LENGTH_MM / mm_per_px,
    )
