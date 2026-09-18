"""Виды корпуса на листе и толщина по ним (план, Ф5).

Базовая линия чтения на корпусах: контур и его размеры модель читает верно, а
толщину — нет (20 вместо 40 и 16 вместо 60), и сама пишет в замечаниях, что
координаты в боковых видах не сходятся с главным. Причина одна: на листе три
вида, и числа приписываются не тому виду. Место элемента — не чтение, а замер.

Здесь виды связываются между собой по проекционной связи (ГОСТ 2.305):

* **план** — прямоугольник ширина × высота (`plate_frame`, ридер их читает
  надёжно);
* **вид спереди** — под планом, в тех же столбцах: его высота и есть толщина;
* **вид слева** — справа от плана, в тех же строках: его ширина — та же
  толщина, второе независимое измерение.

Толщина принимается, только когда оба вида (или единственный найденный)
согласны между собой; иначе — «не измеримо», а не догадка.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Вид в проекционной связи перекрывает сторону плана не меньше чем на эту
# долю: кромка соседнего вида накрывает план целиком, а ДЛИННЕЕ она быть
# может — её удлиняют приливы стенок (корпус G2: 712 px при плане 602).
_ALIGNMENT = 0.9
# Длиннее плана — не больше чем во столько раз: иначе рамкой вида становится
# рамка самого листа.
_MAX_SIDE = 1.6
# Зазор между видами — не меньше этой доли стороны плана: иначе соседней
# «линией вида» становится размерная линия под самим планом.
_MIN_GAP = 0.02
# Два вида согласны о толщине в пределах этой доли.
_AGREEMENT = 0.05


@dataclass(frozen=True)
class HousingViews:
    plan: ViewFrame
    front_bbox_px: tuple[float, float, float, float] | None
    side_bbox_px: tuple[float, float, float, float] | None
    thickness_mm: float | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_bbox_px": [round(float(v), 1) for v in self.plan.bbox_px],
            "front_bbox_px": (
                None
                if self.front_bbox_px is None
                else [round(float(v), 1) for v in self.front_bbox_px]
            ),
            "side_bbox_px": (
                None
                if self.side_bbox_px is None
                else [round(float(v), 1) for v in self.side_bbox_px]
            ),
            "thickness_mm": None if self.thickness_mm is None else round(self.thickness_mm, 3),
            "reason": self.reason,
        }


def locate_housing_views(sheet: Any, width_mm: float, height_mm: float) -> HousingViews | None:
    """Три вида корпуса и толщина по ним; ``None`` — план на листе не найден."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _lines, locate_plate_frame

    plan = locate_plate_frame(sheet, width_mm, height_mm)
    if plan is None:
        return None
    gray = np.asarray(sheet)
    ink = _ink(gray)
    min_length = max(10, int(round(0.02 * min(gray.shape))))
    horizontal = _lines(ink, min_length, axis=0)
    vertical = _lines(ink, min_length, axis=1)
    x0, y0, x1, y1 = (float(v) for v in plan.bbox_px)
    plan_width, plan_height = x1 - x0, y1 - y0

    front = _neighbour_below(horizontal, x0, x1, y1, plan_width, plan_height)
    side = _neighbour_right(vertical, y0, y1, x1, plan_width, plan_height)
    measures = []
    if front is not None:
        measures.append(("вид спереди", (front[1] - front[0]) * plan.scale_v))
    if side is not None:
        measures.append(("вид слева", (side[1] - side[0]) * plan.mm_per_px))
    if not measures:
        return HousingViews(plan, None, None, None, "соседних видов не найдено")
    values = [value for _name, value in measures]
    if len(values) == 2 and abs(values[0] - values[1]) > _AGREEMENT * max(values):
        return HousingViews(
            plan,
            None if front is None else (x0, front[0], x1, front[1]),
            None if side is None else (side[0], y0, side[1], y1),
            None,
            f"виды не согласны о толщине: {values[0]:.2f} и {values[1]:.2f} мм",
        )
    thickness = sum(values) / len(values)
    names = " и ".join(name for name, _value in measures)
    return HousingViews(
        plan,
        None if front is None else (x0, front[0], x1, front[1]),
        None if side is None else (side[0], y0, side[1], y1),
        thickness,
        f"толщина измерена по виду ({names}): {thickness:.2f} мм",
    )


def _neighbour_below(
    horizontal: list, x0: float, x1: float, plan_bottom: float, width: float, height: float
) -> tuple[float, float] | None:
    """Крайние горизонтали вида под планом (его верх и низ), px."""
    gap = _MIN_GAP * height
    levels = [
        line.position
        for line in horizontal
        if line.position > plan_bottom + gap
        and line.overlap(x0, x1) >= _ALIGNMENT * width
        and width * _ALIGNMENT <= (line.end - line.start) <= width * _MAX_SIDE
    ]
    if len(levels) < 2:
        return None
    return min(levels), max(levels)


def _neighbour_right(
    vertical: list, y0: float, y1: float, plan_right: float, width: float, height: float
) -> tuple[float, float] | None:
    """Крайние вертикали вида справа от плана, px."""
    gap = _MIN_GAP * width
    columns = [
        line.position
        for line in vertical
        if line.position > plan_right + gap
        and line.overlap(y0, y1) >= _ALIGNMENT * height
        and height * _ALIGNMENT <= (line.end - line.start) <= height * _MAX_SIDE
    ]
    if len(columns) < 2:
        return None
    return min(columns), max(columns)
