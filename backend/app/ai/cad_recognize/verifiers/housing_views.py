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
# Длина кромки меряется только в окрестности вида (± эта доля его стороны):
# выносные размеров над видом слева продолжают его кромку по той же
# вертикали, и линия «длиннее 1,6 высоты» отбрасывалась как рамка листа —
# вид слева не находился на 4 корпусах из 12. Рамку листа по-прежнему
# отсеивает согласие видов о толщине.
_NEAR = 0.2
# Два вида согласны о толщине в пределах этой доли.
_AGREEMENT = 0.05


@dataclass(frozen=True)
class HousingViews:
    plan: ViewFrame
    front_bbox_px: tuple[float, float, float, float] | None
    side_bbox_px: tuple[float, float, float, float] | None
    thickness_mm: float | None
    reason: str
    # Все виды под планом той же толщины, сверху вниз (разрез и вид спереди).
    below_bboxes_px: tuple[tuple[float, float, float, float], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "below_bboxes_px": [[round(float(v), 1) for v in box] for box in self.below_bboxes_px],
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
    # Все пары уровней под планом с той же толщиной — виды под планом
    # (у корпуса с полостью: разрез, ниже — вид спереди передней стенки).
    stacked: list[tuple[float, float, float, float]] = []
    for low in sorted(front):
        for high in sorted(front):
            if high <= low:
                continue
            if abs((high - low) * plan.scale_v - thickness) <= _AGREEMENT * thickness and all(
                high <= box[1] or low >= box[3] for box in stacked
            ):
                stacked.append((x0, low, x1, high))
    return HousingViews(
        plan,
        None if front_pair is None else (x0, front_pair[0], x1, front_pair[1]),
        None if side_pair is None else (side_pair[0], y0, side_pair[1], y1),
        thickness,
        f"толщина измерена по виду ({names}): {thickness:.2f} мм",
        tuple(sorted(stacked, key=lambda box: box[1])),
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
        and width * _ALIGNMENT
        <= line.overlap(x0 - _NEAR * width, x1 + _NEAR * width)
        <= width * _MAX_SIDE
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
        and height * _ALIGNMENT
        <= line.overlap(y0 - _NEAR * height, y1 + _NEAR * height)
        <= height * _MAX_SIDE
    ]
    return columns


# Масштаб принимается, если сторона вида совпала с надписью листа в пределах
# этой доли (лист рисуется в масштабе, но линия имеет толщину).
_LABEL_TOLERANCE = 0.02
# Кандидат в вид — прямоугольник, стороны которого покрыты основной линией
# не меньше чем на эту долю.
_RECT_COVERAGE = 0.8


def discover_housing_views(
    sheet: Any, labels: list[float], read: tuple[float, float] | None = None
) -> dict[str, Any] | None:
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
    if not below:
        # Разрез корпуса прямоугольником не распознаётся: его контур рвут
        # выступы приливов. Кромки берутся как у соседнего вида — по
        # горизонталям, накрывающим план, с толщиной от вида справа.
        levels = _levels_below(horizontal, x0, x1, y1, width_px, height_px)
        pairs = _spans(levels, 1.0)
        if thickness_px is not None:
            fit = [
                pair for value, pair in pairs if abs(value - thickness_px) <= 0.05 * thickness_px
            ]
            if fit:
                below = [(x0, fit[0][0], x1, fit[0][1])]
        elif pairs:
            thickness_px = pairs[0][0]
            below = [(x0, pairs[0][1][0], x1, pairs[0][1][1])]
    # Сначала — гипотеза модели: если прочитанные ширина и высота ложатся на
    # план (обе, одним масштабом), масштаб берётся от них, а толщина
    # меряется. Живой корпус: 80 × 80 прочитано верно, а среди выписанных
    # надписей не было ни 80, ни 50 — масштаб «по надписям» подобрался по
    # мелким числам (44 × 44 × 27).
    scale = _scale_from_read(read, width_px, height_px)
    if scale is None:
        scale = _scale_by_labels([width_px, height_px, thickness_px], labels, _stamp_scales(gray))
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
    cavity = _cavity(
        gray,
        rectangles,
        plan,
        None if not below else min(below, key=lambda b: b[1]),
        None if not right else min(right, key=lambda b: b[0]),
        scale,
        line_px,
        numbers,
        stated,
    )
    return {
        "cavity": cavity,
        "plan_bbox_px": [round(float(v), 1) for v in plan],
        "front_bbox_px": (
            None if not below else [round(float(v), 1) for v in min(below, key=lambda b: b[1])]
        ),
        "side_bbox_px": (
            None if not right else [round(float(v), 1) for v in min(right, key=lambda b: b[0])]
        ),
        # Все виды под планом сверху вниз: у корпуса с полостью первым идёт
        # разрез, передняя стенка — на виде спереди под ним.
        "below_bboxes_px": [
            [round(float(v), 1) for v in box] for box in sorted(below, key=lambda b: b[1])
        ],
        "mm_per_px": round(scale, 6),
        "width_mm": stated(width_px),
        "height_mm": stated(height_px),
        "thickness_mm": stated(thickness_px),
    }


def _cavity(
    gray: Any,
    rectangles: list[tuple[float, float, float, float]],
    plan: tuple[float, float, float, float],
    section: tuple[float, float, float, float] | None,
    side: tuple[float, float, float, float] | None,
    scale: float,
    line_px: float,
    numbers: list[float],
    stated,
) -> dict[str, Any] | None:
    """Полость корпуса по РАЗРЕЗУ: ширина и глубина — с него, высота — с вида слева.

    Ридер полость не выписывает вовсе (живой корпус: спек без единого элемента
    грани). В плане её кромки штриховые — прямыми их не найти; в разрезе она
    открыта вниз и видна прямоугольником, в виде слева — тем же способом.
    Числа берутся с надписей листа.
    """
    if section is None:
        return None
    # Полость открыта наружу: её прямоугольник упирается в ОДНУ кромку разреза,
    # а не в обе — иначе «глубиной» становится вся толщина (40 вместо 32).
    inner = [
        box
        for box in rectangles
        if box[0] > section[0] + 1
        and box[2] < section[2] - 1
        and box[1] >= section[1] - 1
        and box[3] <= section[3] + 1
        and (box[2] - box[0]) < 0.95 * (section[2] - section[0])
        and ((abs(box[1] - section[1]) <= 2.0) != (abs(box[3] - section[3]) <= 2.0))
    ]
    if not inner:
        return None
    box = max(inner, key=lambda b: b[2] - b[0])
    width_px = box[2] - box[0]
    depth_px = box[3] - box[1]
    # Ширина и глубина — из разреза; высота и положение полости — по штриховым
    # кромкам плана (рамка разреза включает выступы приливов, и центр по ней
    # уезжал на 4 мм).
    columns = _dashed_edges(
        gray, plan, axis="u", line_px=line_px, scale=scale, numbers=numbers, want_px=width_px
    )
    rows = _dashed_edges(
        gray,
        plan,
        axis="v",
        line_px=line_px,
        scale=scale,
        numbers=numbers,
        inside=None if columns is None else columns,
    )
    height_px = None if rows is None else rows[1] - rows[0]
    plan_width, plan_height = plan[2] - plan[0], plan[3] - plan[1]
    if width_px < 0.2 * plan_width or (height_px is not None and height_px < 0.2 * plan_height):
        return None
    centre_u = (
        0.0
        if columns is None
        else round(((columns[0] + columns[1]) / 2.0 - (plan[0] + plan[2]) / 2.0) * scale, 2)
    )
    centre_v = (
        0.0
        if rows is None
        else round(((plan[1] + plan[3]) / 2.0 - (rows[0] + rows[1]) / 2.0) * scale, 2)
    )
    return {
        "bbox_px": [round(float(v), 1) for v in box],
        "width_mm": stated(width_px),
        "height_mm": None if height_px is None else stated(height_px),
        "depth_mm": stated(depth_px),
        "center_u_mm": centre_u,
        "center_v_mm": centre_v,
    }


def _dashed_edges(
    gray: Any,
    plan: tuple[float, float, float, float],
    *,
    axis: str,
    line_px: float,
    scale: float,
    numbers: list[float],
    want_px: float | None = None,
    inside: tuple[float, float] | None = None,
) -> tuple[float, float] | None:
    """Штриховые кромки полости в плане: (первая, вторая) в пикселях вида.

    В плане полость скрыта: её кромки — штриховые линии, прямыми их не найти
    (в разрезе видны только ширина и глубина). Штрих остаётся чернилами, но
    чернила дают и отверстия, и приливы — поэтому из кандидатов берётся пара,
    расстояние между которой совпало с НАДПИСЬЮ листа (или с уже измеренной
    стороной): геометрия замером, число надписью.
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    x0, y0, x1, y1 = (float(value) for value in plan)
    margin = int(max(3.0, 3.0 * line_px))
    ink = _ink(np.asarray(gray))
    if axis == "v":
        low, high = int(y0) + margin, int(y1) - margin
        left = int(inside[0]) if inside else int(x0) + margin
        right = int(inside[1]) if inside else int(x1) - margin
        band = ink[low:high, max(left, 0) : right]
        coverage = band.mean(axis=1) if band.size else None
        origin = low
    else:
        left, right = int(x0) + margin, int(x1) - margin
        low = int(inside[0]) if inside else int(y0) + margin
        high = int(inside[1]) if inside else int(y1) - margin
        band = ink[max(low, 0) : high, left:right]
        coverage = band.mean(axis=0) if band.size else None
        origin = left
    if coverage is None or coverage.size == 0 or not numbers:
        return None
    candidates = [int(value) for value in np.nonzero(coverage >= 0.3)[0]]
    if len(candidates) < 2:
        return None
    best: tuple[float, float, float] | None = None
    for index, first in enumerate(candidates):
        for second in candidates[index + 1 :]:
            span = float(second - first)
            if span < 5:
                continue
            if want_px is not None:
                if abs(span - want_px) > max(3.0, 0.03 * want_px):
                    continue
            else:
                nearest = min(numbers, key=lambda n: abs(n - span * scale))
                if abs(nearest - span * scale) > _LABEL_TOLERANCE * nearest:
                    continue
            weight = float(coverage[first] + coverage[second])
            if best is None or (weight, span) > (best[0], best[1]):
                best = (weight, span, float(first))
    if best is None:
        return None
    return origin + best[2], origin + best[2] + best[1]


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


def _scale_from_read(
    read: tuple[float, float] | None, width_px: float, height_px: float
) -> float | None:
    """Масштаб от прочитанных габаритов — если план их подтверждает."""
    if not read or not all(isinstance(v, (int, float)) and v > 0 for v in read):
        return None
    read_w, read_h = float(read[0]), float(read[1])
    for a, b in ((read_w, read_h), (read_h, read_w)):
        scale_u, scale_v = a / width_px, b / height_px
        if abs(scale_u / scale_v - 1.0) <= _LABEL_TOLERANCE:
            return (scale_u + scale_v) / 2.0
    return None


def _stamp_scales(gray: Any) -> list[float]:
    """Масштабы листа по основной надписи ЕСКД (ряд ГОСТ 2.302), мм детали на px.

    Надписи, которые выписал ридер, — ненадёжная опора для масштаба: на живом
    корпусе габарита среди них не было, и масштаб выбрался по мелким числам
    (принято 44 × 44 × 27 при 80 × 80 × 50). Штамп 185 × 55 мм — линейка
    БУМАГИ, и вместе с рядом стандартных масштабов даёт короткий список
    допустимых мм/px, не зависящий от чтения.
    """
    from app.ai.cad_recognize.verifiers.sheet_scale import _GOST_SCALES, locate_title_block

    block = locate_title_block(gray)
    if block is None or block.paper_px_per_mm <= 0:
        return []
    paper_mm_per_px = 1.0 / block.paper_px_per_mm
    return [paper_mm_per_px * paper / model for model, paper in _GOST_SCALES]


def _scale_by_labels(
    spans_px: list[float | None], labels: list[float], allowed: list[float] | None = None
) -> float | None:
    """Масштаб, при котором стороны видов ложатся на надписи листа.

    Кандидаты берутся по сторонам ПЛАНА, и обе его стороны обязаны лечь на
    надписи: надписей на листе много (координаты, размеры элементов), и
    половинный масштаб тоже «совпадал» — корпус выходил 40 × 40 при 80 × 80.
    Если штамп листа дал ряд стандартных масштабов (``allowed``), кандидат
    обязан быть одним из них: на живом корпусе среди прочитанных надписей не
    было габарита, и без этой привязки принималось 44 × 44 × 27 при
    80 × 80 × 50. При равном числе совпадений берётся больший масштаб: тело —
    самый большой объект листа.
    """
    spans = [value for value in spans_px if value]
    plan_sides = [value for value in spans_px[:2] if value]
    numbers = sorted({float(value) for value in labels if value and float(value) > 1.0})
    if len(plan_sides) < 2 or not numbers:
        return None

    def nearest(value: float) -> tuple[float, float]:
        best = min(numbers, key=lambda n: abs(n - value))
        return best, abs(best - value) / max(best, 1e-6)

    def on_ladder(scale: float) -> bool:
        return not allowed or any(
            abs(scale / candidate - 1.0) <= _LABEL_TOLERANCE for candidate in allowed
        )

    best: tuple[int, float, float] | None = None
    for side in plan_sides:
        for number in numbers:
            scale = number / side
            if not on_ladder(scale):
                continue
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
