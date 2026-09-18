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

    front = _levels_below(horizontal, x0, x1, y1, plan_width, plan_height)
    side = _levels_right(vertical, y0, y1, x1, plan_width, plan_height)
    chosen = _agreed_pair(front, side, plan.scale_v, plan.mm_per_px)
    if chosen is None:
        return HousingViews(plan, None, None, None, "соседних видов не найдено")
    front_pair, side_pair, thickness, names = chosen
    if thickness is None:
        return HousingViews(
            plan,
            None if front_pair is None else (x0, front_pair[0], x1, front_pair[1]),
            None if side_pair is None else (side_pair[0], y0, side_pair[1], y1),
            None,
            names,
        )
    return HousingViews(
        plan,
        None if front_pair is None else (x0, front_pair[0], x1, front_pair[1]),
        None if side_pair is None else (side_pair[0], y0, side_pair[1], y1),
        thickness,
        f"толщина измерена по виду ({names}): {thickness:.2f} мм",
    )


def _agreed_pair(front: list[float], side: list[float], scale_v: float, scale_u: float):
    """Кромки тела на соседних видах, согласованные между собой.

    Крайние линии вида — не всегда кромки тела: у корпуса housing-7 в вид
    спереди попадала лишняя линия, и «толщина» выходила 90 мм при 60. Когда
    оба вида нашлись, выбирается пара, о которой они согласны.
    """
    front_spans = _spans(front, scale_v)
    side_spans = _spans(side, scale_u)
    if front_spans and side_spans:
        best = min(
            (
                (abs(a_mm - b_mm) / max(a_mm, b_mm), a_mm, b_mm, a, b)
                for a_mm, a in front_spans
                for b_mm, b in side_spans
            ),
            key=lambda item: (item[0], -min(item[1], item[2])),
        )
        error, a_mm, b_mm, a, b = best
        if error <= _AGREEMENT:
            return a, b, (a_mm + b_mm) / 2.0, "вид спереди и вид слева"
        return (
            None,
            None,
            None,
            f"виды не согласны о толщине: {front_spans[0][0]:.2f} и {side_spans[0][0]:.2f} мм",
        )
    if front_spans:
        span_mm, pair = front_spans[0]
        return pair, None, span_mm, "вид спереди"
    if side_spans:
        span_mm, pair = side_spans[0]
        return None, pair, span_mm, "вид слева"
    return None


def _spans(levels: list[float], scale: float) -> list[tuple[float, tuple[float, float]]]:
    """Пары кромок вида, от самой широкой: у тела это его габарит по толщине."""
    pairs = [
        ((high - low) * scale, (low, high))
        for index, low in enumerate(sorted(levels))
        for high in sorted(levels)[index + 1 :]
    ]
    return sorted(pairs, key=lambda item: -item[0])


def _levels_below(
    horizontal: list, x0: float, x1: float, plan_bottom: float, width: float, height: float
) -> list[float]:
    """Горизонтали вида под планом — кандидаты в кромки тела, px."""
    gap = _MIN_GAP * height
    levels = [
        line.position
        for line in horizontal
        if line.position > plan_bottom + gap
        and line.overlap(x0, x1) >= _ALIGNMENT * width
        and width * _ALIGNMENT <= (line.end - line.start) <= width * _MAX_SIDE
    ]
    return levels


def _levels_right(
    vertical: list, y0: float, y1: float, plan_right: float, width: float, height: float
) -> list[float]:
    """Вертикали вида справа от плана — кандидаты в кромки тела, px."""
    gap = _MIN_GAP * width
    columns = [
        line.position
        for line in vertical
        if line.position > plan_right + gap
        and line.overlap(y0, y1) >= _ALIGNMENT * height
        and height * _ALIGNMENT <= (line.end - line.start) <= height * _MAX_SIDE
    ]
    return columns


# Масштаб принимается, если сторона вида совпала с надписью листа в пределах
# этой доли (лист рисуется в масштабе, но линия имеет толщину).
_LABEL_TOLERANCE = 0.02
# Кандидат в вид — прямоугольник, стороны которого покрыты основной линией
# не меньше чем на эту долю.
_RECT_COVERAGE = 0.8


def discover_housing_views(sheet: Any, labels: list[float]) -> dict[str, Any] | None:
    """Корпус по листу: три вида по геометрии, размеры — по надписям (Ф5).

    Когда габариты прочитаны неверно, план по ним не найти: живой корпус
    прочитан «пластиной» 50 × 80 × 16 при 100 × 100 × 50, и проверка честно
    отвечала «не измеримо». Здесь виды ищутся сами: самый большой замкнутый
    прямоугольник основной линии — план, под ним и справа от него — виды в
    проекционной связи. Масштаб выбирается тот, при котором стороны видов
    ложатся на надписи листа: числа — с листа, геометрия — замером.
    """
    import numpy as np

    from app.ai.cad_recognize.sheet_upscale import main_line_px
    from app.ai.cad_recognize.verifiers.flange_outline import _main_lines
    from app.ai.cad_recognize.verifiers.plate_frame import _lines

    gray = np.asarray(sheet)
    line_px = main_line_px(gray)
    if line_px <= 0:
        return None
    main = _main_lines(gray, line_px) > 0
    min_length = max(10, int(round(0.05 * min(gray.shape))))
    horizontal = _lines(main, min_length, axis=0)
    vertical = _lines(main, min_length, axis=1)
    rectangles = _rectangles(horizontal, vertical)
    if not rectangles:
        return None
    plan = max(rectangles, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
    x0, y0, x1, y1 = plan
    width_px, height_px = x1 - x0, y1 - y0
    below = [
        box
        for box in rectangles
        if box[1] > y1 + 0.02 * height_px
        and min(box[2], x1) - max(box[0], x0) >= _ALIGNMENT * width_px
    ]
    right = [
        box
        for box in rectangles
        if box[0] > x1 + 0.02 * width_px
        and min(box[3], y1) - max(box[1], y0) >= _ALIGNMENT * height_px
    ]
    thickness_px = None
    if below:
        thickness_px = min(box[3] - box[1] for box in below)
    if right:
        side_px = min(box[2] - box[0] for box in right)
        thickness_px = side_px if thickness_px is None else (thickness_px + side_px) / 2.0
    scale = _scale_by_labels([width_px, height_px, thickness_px], labels)
    if scale is None:
        return None

    def stated(value: float | None) -> float | None:
        """Число берётся с НАДПИСИ листа, геометрия — замером (принцип плана)."""
        if value is None:
            return None
        measured = value * scale
        nearest = min(numbers, key=lambda n: abs(n - measured)) if numbers else None
        if nearest is not None and abs(nearest - measured) <= _LABEL_TOLERANCE * nearest:
            return round(float(nearest), 3)
        return round(measured, 2)

    numbers = sorted({float(value) for value in labels if value and float(value) > 1.0})
    return {
        "plan_bbox_px": [round(float(v), 1) for v in plan],
        "front_bbox_px": (
            None if not below else [round(float(v), 1) for v in min(below, key=lambda b: b[1])]
        ),
        "side_bbox_px": (
            None if not right else [round(float(v), 1) for v in min(right, key=lambda b: b[0])]
        ),
        "mm_per_px": round(scale, 6),
        "width_mm": stated(width_px),
        "height_mm": stated(height_px),
        "thickness_mm": stated(thickness_px),
    }


def _rectangles(horizontal: list, vertical: list) -> list[tuple[float, float, float, float]]:
    """Замкнутые прямоугольники основной линии: (x0, y0, x1, y1)."""
    boxes = []
    for index, top in enumerate(horizontal):
        for bottom in horizontal[index + 1 :]:
            low, high = sorted((top.position, bottom.position))
            if high - low < 10:
                continue
            columns = [
                line
                for line in vertical
                if line.overlap(low, high) >= _RECT_COVERAGE * (high - low)
            ]
            for i, left in enumerate(columns):
                for right in columns[i + 1 :]:
                    a, b = sorted((left.position, right.position))
                    if b - a < 10:
                        continue
                    if top.overlap(a, b) >= _RECT_COVERAGE * (b - a) and bottom.overlap(
                        a, b
                    ) >= _RECT_COVERAGE * (b - a):
                        boxes.append((a, low, b, high))
    return boxes


def _scale_by_labels(spans_px: list[float | None], labels: list[float]) -> float | None:
    """Масштаб, при котором стороны видов ложатся на надписи листа.

    Кандидаты берутся по сторонам ПЛАНА, и обе его стороны обязаны лечь на
    надписи: надписей на листе много (координаты, размеры элементов), и
    половинный масштаб тоже «совпадал» — корпус выходил 40 × 40 при 80 × 80.
    При равном числе совпадений берётся больший масштаб: тело — самый большой
    объект листа, и меньший масштаб описывает его же элемент.
    """
    spans = [value for value in spans_px if value]
    plan_sides = [value for value in spans_px[:2] if value]
    numbers = sorted({float(value) for value in labels if value and float(value) > 1.0})
    if len(plan_sides) < 2 or not numbers:
        return None

    def nearest(value: float) -> tuple[float, float]:
        best = min(numbers, key=lambda n: abs(n - value))
        return best, abs(best - value) / max(best, 1e-6)

    best: tuple[int, float, float] | None = None
    for side in plan_sides:
        for number in numbers:
            scale = number / side
            fits = [nearest(other * scale) for other in plan_sides]
            if any(relative > _LABEL_TOLERANCE for _value, relative in fits):
                continue
            hits = sum(1 for other in spans if nearest(other * scale)[1] <= _LABEL_TOLERANCE)
            error = sum(relative for _value, relative in fits)
            if best is None or (hits, scale, -error) > (best[0], best[2], -best[1]):
                best = (hits, error, scale)
    if best is None:
        return None
    return best[2]
