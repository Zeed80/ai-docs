"""Координационные оси плана и масштаб листа (план, Ф7.1).

Стены на плане измеримы только вместе с масштабом: «толщина 27 px» — это и
перегородка, и наружная стена, смотря какой лист. Масштаб задаёт сам чертёж:
маркеры координационных осей (кружки с буквой или цифрой по ГОСТ 21.101) стоят
на известных расстояниях, и эти расстояния подписаны размерной цепочкой.

Маркеры находит геометрия, число между ними читает модель по вырезу — тот же
приём, что у отметок уровня (`construction_levels`): выдумывать «6000» ей
неоткуда, когда вырез показывает одно звено цепочки. Масштаб принимается,
только если согласны не меньше двух звеньев: одно звено — это догадка.
"""

from __future__ import annotations

import re
from typing import Any

# Кружок оси по ГОСТ 21.101 — 6…12 мм на листе; на растре 300 dpi это
# 70…140 px, на уменьшенном рендере меньше. Радиус ищется в широком окне, а
# лишнее отсекает «все маркеры одного размера».
_MIN_RADIUS_SHARE = 0.002
_MAX_RADIUS_SHARE = 0.02
# Маркеры одного ряда стоят на одной линии: разброс поперёк — доля радиуса.
_ROW_TOLERANCE = 1.5
# Согласие звеньев цепочки: масштабы не должны расходиться больше чем на это.
_SCALE_AGREEMENT = 0.05
# Доля окружности, покрытая чернилами, чтобы это была обводка маркера.
_RING_COVERAGE = 0.85
# Ряд маркеров тянется вдоль здания: доля длинной стороны листа.
_ROW_SPAN_SHARE = 0.25
# Доля чернил внутри маркера: буква или цифра, а не пустота и не рисунок.
_LABEL_INK = (0.04, 0.45)
# Дыра маркера — круг: её площадь от площади описанной окружности.
_HOLE_ROUNDNESS = 0.8

CHAIN_LABEL_PROMPT = (
    "На фрагменте — участок размерной цепочки между двумя координационными "
    "осями плана (кружки с буквой или цифрой). Какое число стоит на этом "
    "участке цепочки — расстояние между осями в миллиметрах? Только число с "
    'листа, целиком. ОДНОЙ строкой JSON: {"span_mm": 6000} или {"span_mm": null}. '
    "Только JSON."
)
CHAIN_LABEL_SCHEMA = {"type": "object", "properties": {"span_mm": {"type": ["number", "null"]}}}

_SPAN_MM = re.compile(r"^\d{3,5}$")


def axis_markers(gray: Any) -> list[tuple[float, float, float]]:
    """Кружки координационных осей: (x, y, радиус), px.

    Берутся окружности ОДНОГО размера: на плане их много и они одинаковы, а
    случайная дуга или кружок отметки — один.
    """
    import numpy as np

    gray = np.asarray(gray)
    reach = max(gray.shape)
    ink = np.asarray(gray) < 160
    circles = [
        (x, y, r)
        for x, y, r in _ring_holes(ink, _MIN_RADIUS_SHARE * reach, _MAX_RADIUS_SHARE * reach)
        if _ring_drawn(ink, x, y, r) and _holds_a_label(ink, x, y, r)
    ]
    if len(circles) < 2:
        return []
    return _grid_sized(circles, reach)


def _grid_sized(
    circles: list[tuple[float, float, float]], reach: float
) -> list[tuple[float, float, float]]:
    """Из кружков одного размера — те, чей РЯД тянется через лист.

    Ни «самый частый радиус», ни «больше всего в рядах» не годятся: мелких
    ложных кругов на плане больше, чем маркеров (перекрытия: 14 против 7), и
    настоящие отбрасывались все до одного. Сетка осей узнаётся тем, что её
    ряд идёт вдоль всего здания, а случайные кружки стоят кучкой.
    """
    best: tuple[float, float, list[tuple[float, float, float]]] | None = None
    for candidate in circles:
        radius = candidate[2]
        same = [item for item in circles if abs(item[2] - radius) <= 0.25 * radius]
        spans = []
        for row in marker_rows(same):
            axis = 0 if abs(row[0][0] - row[-1][0]) >= abs(row[0][1] - row[-1][1]) else 1
            spans.append(abs(row[-1][axis] - row[0][axis]) / reach)
        share = round(max(spans, default=0.0), 2)
        key = (share, radius)
        if best is None or key > (best[0], best[1]):
            best = (share, radius, same)
    return best[2] if best and best[0] >= _ROW_SPAN_SHARE else []


def _ring_holes(ink: Any, min_radius: float, max_radius: float) -> list[tuple[float, float, float]]:
    """Кружки как КОЛЬЦА: дыра в чернилах, круглая по форме.

    Хаф по листу с заштрихованными стенами давал сотни «окружностей» на
    штриховке и терял настоящие маркеры (синтетический план: 33 из 145 при
    342 лишних). У маркера внутренняя граница обводки — замкнутый круг, буква
    внутри — отдельный островок и форму дыры не портит. Радиус — до середины
    обводки (внутренний плюс половина её толщины), как у Хафа.
    """
    import math

    import cv2
    import numpy as np

    # Тонкая сглаженная обводка (1 px в DWG) по порогу рвётся — дыра не
    # замыкается; расширение на пиксель её замыкает, а штриховку круглой
    # не делает.
    closed = cv2.dilate(ink.astype("uint8"), np.ones((3, 3), np.uint8))
    contours, hierarchy = cv2.findContours(closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hierarchy is None:
        return []
    found = []
    for contour, (_next, _previous, _child, parent) in zip(contours, hierarchy[0], strict=False):
        if parent < 0:
            continue  # внешняя граница компоненты, а не дыра
        (x, y), radius = cv2.minEnclosingCircle(contour)
        if not min_radius * 0.8 <= radius <= max_radius:
            continue
        if cv2.contourArea(contour) < _HOLE_ROUNDNESS * math.pi * radius * radius:
            continue
        found.append((float(x), float(y), _stroke_middle(ink, float(x), float(y), float(radius))))
    return found


def _stroke_middle(ink: Any, x: float, y: float, inner: float) -> float:
    """Радиус до середины обводки: от внутренней границы — лучами наружу до
    конца чернил. Вырез звена цепочки строится от этого радиуса; внутренний
    на толстой обводке DWG сужал вырез, и число цепочки в него не попадало."""
    import math

    widths = []
    for step in range(16):
        angle = 2.0 * math.pi * step / 16
        width = 0
        for distance in range(int(inner), int(inner * 1.6) + 2):
            column = int(round(x + distance * math.cos(angle)))
            row = int(round(y + distance * math.sin(angle)))
            if not (0 <= row < ink.shape[0] and 0 <= column < ink.shape[1]):
                break
            if ink[row, column]:
                width = distance - inner + 1
            elif width:
                break
        if width:
            widths.append(width)
    widths.sort()
    stroke = widths[len(widths) // 2] if widths else 2.0
    return inner + stroke / 2.0


def _ring_drawn(ink: Any, x: float, y: float, radius: float) -> bool:
    """Обводка есть по всей окружности: Хаф отзывается и на дугу, и на угол."""
    import math

    hits = 0
    for step in range(48):
        angle = 2.0 * math.pi * step / 48
        column = int(round(x + radius * math.cos(angle)))
        row = int(round(y + radius * math.sin(angle)))
        if 0 <= row < ink.shape[0] and 0 <= column < ink.shape[1]:
            hits += bool(ink[max(0, row - 2) : row + 3, max(0, column - 2) : column + 3].any())
    return hits >= _RING_COVERAGE * 48


def _holds_a_label(ink: Any, x: float, y: float, radius: float) -> bool:
    """Внутри маркера — короткая надпись: не пусто и не плотный рисунок."""
    reach = int(round(0.6 * radius))
    row, column = int(round(y)), int(round(x))
    inside = ink[max(0, row - reach) : row + reach + 1, max(0, column - reach) : column + reach + 1]
    if inside.size == 0:
        return False
    share = float(inside.mean())
    return _LABEL_INK[0] <= share <= _LABEL_INK[1]


def marker_rows(
    markers: list[tuple[float, float, float]],
) -> list[list[tuple[float, float, float]]]:
    """Ряды маркеров: горизонтальный ряд (оси 1, 2, 3…) и вертикальный (А, Б…)."""
    rows: list[list[tuple[float, float, float]]] = []
    for axis in (0, 1):
        grouped: dict[int, list[tuple[float, float, float]]] = {}
        for marker in markers:
            key = round(marker[1 - axis] / (_ROW_TOLERANCE * marker[2]))
            grouped.setdefault(key, []).append(marker)
        for row in grouped.values():
            if len(row) >= 2:
                rows.append(sorted(row, key=lambda item: item[axis]))
    return rows


def chain_crop_box(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
    axis: int,
    size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Вырез со звеном цепочки между двумя маркерами: от кружка до кружка и
    полоса в сторону детали, где стоит размерная линия с числом."""
    reach = 2.5 * first[2]
    if axis == 0:
        left, right = first[0], second[0]
        middle = (first[1] + second[1]) / 2.0
        box = (left - reach, middle - reach, right + reach, middle + reach)
    else:
        top, bottom = first[1], second[1]
        middle = (first[0] + second[0]) / 2.0
        box = (middle - reach, top - reach, middle + reach, bottom + reach)
    return (
        max(0, int(box[0])),
        max(0, int(box[1])),
        min(size[0], int(box[2])),
        min(size[1], int(box[3])),
    )


def scale_from_spans(
    spans: list[tuple[float, float]], *, attempted: int | None = None
) -> float | None:
    """Масштаб (мм на пиксель) по звеньям ``(px, mm)``, если звенья согласны.

    Одно звено — догадка: модель могла прочитать соседнее число или подпись
    оси. Принимается медиана, вокруг которой не меньше двух звеньев.
    """
    ratios = sorted(mm / px for px, mm in spans if px > 0 and mm > 0)
    if len(ratios) < 2:
        return None
    # Согласная группа обязана быть БОЛЬШИНСТВОМ звеньев. Живой план «на отм.
    # 0.000» нарисован не в масштабе (4500 мм — 112 px, 7500 — 142 px): из
    # шести звеньев два случайно сошлись на 14 мм/px, и по «согласию двух»
    # замер принял масштаб и нашёл 247 «стен» — рамки отметок, ступени, текст.
    agreeing = max(
        (
            [value for value in ratios if abs(value - center) <= _SCALE_AGREEMENT * center]
            for center in ratios
        ),
        key=len,
    )
    # Большинство — от ВСЕХ звеньев, которые пробовали прочитать: на том же
    # скане с другими маркерами прочитались лишь два звена слева (оба 3000),
    # «два из двух» приняли масштаб листа, нарисованного не в масштабе.
    if len(agreeing) < 2 or 2 * len(agreeing) <= max(len(ratios), attempted or 0):
        return None
    return round(sum(agreeing) / len(agreeing), 5)


async def read_sheet_scale(image_bytes: bytes, *, ask: Any = None) -> dict[str, Any]:
    """Масштаб плана по цепочке между осями: {mm_per_px, spans, markers}."""
    import io

    import numpy as np
    from PIL import Image

    if ask is None:
        ask = _default_ask
    Image.MAX_IMAGE_PIXELS = None
    sheet = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    markers = axis_markers(np.asarray(sheet.convert("L")))
    spans: list[dict[str, Any]] = []
    attempted = 0
    for row in marker_rows(markers):
        axis = 0 if abs(row[0][0] - row[-1][0]) >= abs(row[0][1] - row[-1][1]) else 1
        for first, second in zip(row, row[1:], strict=False):
            distance = abs(second[axis] - first[axis])
            if distance < 4.0 * first[2]:
                continue
            crop = chain_crop_box(first, second, axis, sheet.size)
            attempted += 1
            answer = await ask(CHAIN_LABEL_PROMPT, sheet.crop(crop))
            value = (answer or {}).get("span_mm")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if not _SPAN_MM.match(f"{round(float(value))}"):
                continue
            spans.append({"px": round(distance, 1), "mm": float(value), "bbox_px": list(crop)})
    scale = scale_from_spans([(item["px"], item["mm"]) for item in spans], attempted=attempted)
    return {
        "mm_per_px": scale,
        "markers": [[round(value, 1) for value in marker] for marker in markers],
        "spans": spans,
        "attempted": attempted,
        "reason": None
        if scale
        else (
            "осей на листе не найдено"
            if len(markers) < 2
            else "числа цепочки между осями не прочитаны"
            if len(spans) < 2
            else f"прочитано звеньев цепочки {len(spans)} из {attempted} — масштаб не подтверждён"
            if 2 * len(spans) <= attempted
            else "звенья цепочки между осями не согласны — лист не в масштабе"
        ),
    }


async def _default_ask(prompt: str, image: Any) -> dict:
    from app.ai.cad_recognize.spec_fragments import _ask, _overview
    from app.ai.router import ai_router

    return await _ask(
        prompt,
        _overview(image),
        router=ai_router,
        confidential=True,
        num_predict=120,
        schema=CHAIN_LABEL_SCHEMA,
        timeout_seconds=60.0,
    )
