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
планов — они в прямоугольной рамке, другое обозначение: для них
``level_boxes``.
"""

from __future__ import annotations

import math
import re
from typing import Any

# Штрих стрелки — под 45° с допуском, короткий.
_DIAGONAL_TOLERANCE_DEG = 12.0
_VERTICAL_TOLERANCE_DEG = 6.0


# Плитки поиска знаков, px. Детектор отрезков на целом листе 12 000 px
# теряет часть мелких знаков: на фасаде 1-3 из стопки «7,750 / 6,550 /
# 5,350» по целому листу находился один, по вырезу — все (11 → 18 знаков).
_TILE = 1500
_TILE_OVERLAP = 300


def level_marks(gray: Any) -> list[tuple[int, int, int, int]]:
    """Знаки отметок: (x, y) вершины стрелки, длина штриха, px, и сторона полки
    (−1 — выше вершины, +1 — ниже)."""
    import numpy as np

    gray = np.asarray(gray)
    height, width = gray.shape[:2]
    marks: list[tuple[int, int, int, int]] = []
    step = _TILE - _TILE_OVERLAP
    for top in range(0, max(1, height - _TILE_OVERLAP), step):
        for left in range(0, max(1, width - _TILE_OVERLAP), step):
            for x, y, stroke, side in _marks_in(gray[top : top + _TILE, left : left + _TILE]):
                mark = (x + left, y + top, stroke, side)
                if all(math.hypot(mark[0] - m[0], mark[1] - m[1]) > 10 for m in marks):
                    marks.append(mark)
    return marks


def _marks_in(gray: Any) -> list[tuple[int, int, int, int]]:
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
    marks: list[tuple[int, int, int, int]] = []
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
            if (
                any(
                    math.hypot(p[0] - vx, p[1] - vy) <= 0.4 * reach + 3.0
                    for v in vertical
                    for p in ends(v)
                )
                and _stem_px(ink, vx, vy, tail_a[0][1] - vy) <= _STEM_STROKES * reach
            ):
                # Четвёртое — сторона полки: −1 выше вершины, +1 ниже.
                mark = (
                    round(vx),
                    round(vy),
                    round(float(lengths[a])),
                    1 if tail_a[0][1] > vy else -1,
                )
                # Вершина внутри толстого штриха — это глиф крупного текста
                # (изгибы «3» дают штрихи под 45° и вертикаль), а не знак из
                # тонких линий: плотность чернил у вершины у знаков ≤ 0,29, у
                # цифры «3» — 0,80 (план «на отм. 0.000»: выдуманное «+3.360»).
                r = max(3, mark[2] // 2)
                around = ink[
                    max(0, mark[1] - r) : mark[1] + r + 1, max(0, mark[0] - r) : mark[0] + r + 1
                ]
                if around.size and float((around > 0).mean()) > _GLYPH_DENSITY:
                    continue
                if all(math.hypot(mark[0] - m[0], mark[1] - m[1]) > 10 for m in marks):
                    marks.append(mark)
    return marks


# Черта знака отметки — от стрелки до полки, несколько длин штриха стрелки.
# Стрелка вертикальной РАЗМЕРНОЙ линии выглядит так же (две половинки и
# вертикаль), но её черта тянется через весь размер: «3,950» и «+3,360»
# на фасаде и плане — числа размеров у таких стрелок.
_STEM_STROKES = 6.0
_GLYPH_DENSITY = 0.5


def _stem_px(ink: Any, x: float, y: float, direction: float) -> float:
    """Длина непрерывной вертикали чернил от вершины стрелки в сторону штрихов."""
    step = 1 if direction > 0 else -1
    column = int(round(x))
    height = ink.shape[0]
    row, gap, length = int(round(y)), 0, 0
    while 0 <= row < height and gap <= 2:
        if ink[row, max(0, column - 1) : column + 2].any():
            length += 1 + gap
            gap = 0
        else:
            gap += 1
        row += step
    return float(length)


LEVEL_AT_MARK_PROMPT = (
    "В центре фрагмента — знак отметки уровня (стрелка, упёртая в линию, и "
    "полка). Какое число стоит на полке ЭТОГО знака? Отметка в метрах с тремя "
    "знаками после точки, знак «+» или «−» как на листе. Если числа у знака "
    'нет — null. ОДНОЙ строкой JSON: {"level": "+3.360"} или {"level": null}. '
    "Только JSON."
)
LEVEL_AT_MARK_SCHEMA = {"type": "object", "properties": {"level": {"type": ["string", "null"]}}}


def mark_crop_box(mark: tuple[int, ...], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Вырез вокруг знака: полка с числом уходит вбок от черты — вдвое шире.

    По вертикали — в сторону полки, за вершину — едва: отметки фасада стоят
    стопкой через ~5 штрихов («7,750 / 6,550 / 5,350»), и симметричный вырез
    захватывал полку соседнего знака — модель отвечала его числом.
    """
    x, y, stroke = mark[:3]
    side = mark[3] if len(mark) > 3 else 0
    reach = max(120, 8 * stroke)
    if side:
        top, bottom = (y - reach, y + stroke) if side < 0 else (y - stroke, y + reach)
    else:
        top, bottom = y - reach, y + reach
    return (
        max(0, x - 2 * reach),
        max(0, top),
        min(size[0], x + 2 * reach),
        min(size[1], bottom),
    )


# Рамка отметки на плане: вытянутый прямоугольник с числом внутри.
_BOX_ASPECT = (2.0, 5.5)
_BOX_INK = (0.03, 0.45)
# Наибольший разрыв угла рамки, который замыкается, px.
_GAP = 21
# Рамка отметки не выше этой доли длинной стороны листа (на рендере
# 12 000 px — 125 px, ~1 %).
_BOX_HEIGHT = 0.04
# Доля площади рамки, которую занимает пустота внутри неё.
_HOLE = 0.15


def level_boxes(gray: Any) -> list[tuple[int, int, int, int]]:
    """Отметки планов в прямоугольной рамке (ГОСТ 21.101): (x0, y0, x1, y1), px.

    На плане знак отметки — не стрелка, а число в тонкой рамке. Рамка —
    замкнутый контур из четырёх вершин вдоль осей, вытянутый в 2–5,5 раза,
    внутри — текст (доля чернил), рядом с рамкой её же линии не продолжаются.
    Что в рамке именно отметка, решает узкий вопрос и формат числа: рамки
    бывают и у других надписей.
    """
    import cv2
    import numpy as np

    gray = np.asarray(gray)
    ink = (gray < 160).astype(np.uint8)
    # Контуры — только по линиям вдоль осей: выноска, пересекающая рамку
    # (план «на отм. 0 и −6780», −1.800), делила её на куски, и рамка не
    # находилась. Текст внутри остаётся в ``ink`` для доли чернил.
    stroke = 8
    axial = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (stroke, 1))
    ) | cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, stroke))
    )
    # Угол рамки на листе бывает разорван на полтора десятка пикселей (там же,
    # −1.800): замыкание такой «Г»-разрыв не закрывает — эрозия снимает
    # заполненный угол. Линии утолщаются, рамкой служит внутренний край
    # утолщённой, расширенный обратно на половину утолщения.
    axial = cv2.dilate(axial, cv2.getStructuringElement(cv2.MORPH_RECT, (_GAP, _GAP)))
    grow = _GAP // 2
    # Рамка — внешний контур утолщённых линий, у которого внутри ДЫРА заметной
    # площади: утолщённые дуги и полосы тоже прямоугольны снаружи, но пусты
    # внутри. Сама дыра формой не годится: текст вплотную к рамке после
    # утолщения сливается с ней.
    contours, hierarchy = cv2.findContours(axial, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    found: list[tuple[int, int, int, int]] = []
    for index, contour in enumerate(contours):
        if hierarchy is None or hierarchy[0][index][3] >= 0:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        # Прямоугольность — заполнением: у разорванного угла контур даёт
        # лишние вершины, но почти целиком заполняет свою рамку.
        if cv2.contourArea(contour) < 0.9 * w * h:
            continue
        hole, largest = hierarchy[0][index][2], 0.0
        while hole >= 0:
            largest = max(largest, cv2.contourArea(contours[hole]))
            hole = hierarchy[0][hole][0]
        if largest < _HOLE * w * h:
            continue
        x, y, w, h = x + grow, y + grow, w - 2 * grow, h - 2 * grow
        if (
            h < 14
            or h > max(400.0, _BOX_HEIGHT * max(gray.shape))
            or not (_BOX_ASPECT[0] <= w / max(h, 1) <= _BOX_ASPECT[1])
        ):
            continue
        pad = max(2, h // 12)
        inside = ink[max(0, y + pad) : y + h - pad, max(0, x + pad) : x + w - pad]
        if inside.size == 0 or not (_BOX_INK[0] <= float(inside.mean()) <= _BOX_INK[1]):
            continue
        box = (x, y, x + w, y + h)
        if all(abs(box[0] - b[0]) > 4 or abs(box[1] - b[1]) > 4 for b in found):
            found.append(box)
    return found


LEVEL_IN_BOX_PROMPT = (
    "В центре фрагмента — число в прямоугольной рамке. Если это отметка "
    "уровня (метры с тремя знаками после точки: 0.000, -6.760, +3.300), "
    "выпиши её ровно как на листе, со знаком. Если в рамке не отметка — null. "
    'ОДНОЙ строкой JSON: {"level": "-6.760"} или {"level": null}. Только JSON.'
)


def box_crop_box(
    box: tuple[int, int, int, int], size: tuple[int, int]
) -> tuple[int, int, int, int]:
    """Вырез вокруг рамки: сама рамка и полширины вокруг."""
    x0, y0, x1, y1 = box
    reach = max(40, (x1 - x0) // 2)
    return (
        max(0, x0 - reach),
        max(0, y0 - reach),
        min(size[0], x1 + reach),
        min(size[1], y1 + reach),
    )


# Формат отметки: метры с тремя знаками, разделитель — точка или запятая.
_LEVEL_TEXT = re.compile(r"^[+\-−±]?\d{1,3}[.,]\d{3}$")
# Знаки мельче ~12 px не находятся: мелкий лист увеличивается до этого.
_MIN_LONG_SIDE = 5000
# Бюджет узких вопросов на лист.
_MAX_ASKS = 40


def normalize_level(text: str) -> str | None:
    """«+3,360» → «+3.360»; ноль — «0.000»; не отметка — None."""
    value = str(text).strip().replace(" ", "").replace("−", "-").replace(",", ".")
    if not _LEVEL_TEXT.match(value):
        return None
    number = float(value.replace("±", ""))
    return "0.000" if number == 0 else f"{number:+.3f}"


async def read_sheet_levels(image_bytes: bytes, *, ask: Any = None) -> dict[str, Any]:
    """Отметки уровня листа: знак находит геометрия, число читает модель у знака.

    Модель, читающая лист целиком, выдаёт размеры за отметки (E10: 14
    выдуманных на 10 листах). Здесь спрашивается только вырез у найденного
    знака (стрелка с чертой и полкой) или рамки (отметка плана), и ответ
    принимается только в формате отметки: 27 из 29 без выдуманных.
    """
    import io

    import numpy as np
    from PIL import Image

    if ask is None:
        ask = _default_ask
    Image.MAX_IMAGE_PIXELS = None
    sheet = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    factor = 1.0
    if max(sheet.size) < _MIN_LONG_SIDE:
        factor = _MIN_LONG_SIDE / max(sheet.size)
        sheet = sheet.resize(
            (round(sheet.size[0] * factor), round(sheet.size[1] * factor)), resample=3
        )
    gray = np.asarray(sheet.convert("L"))
    places = [
        ("mark", mark_crop_box(mark, sheet.size), LEVEL_AT_MARK_PROMPT)
        for mark in level_marks(gray)
    ] + [("frame", box_crop_box(box, sheet.size), LEVEL_IN_BOX_PROMPT) for box in level_boxes(gray)]
    levels: list[dict[str, Any]] = []
    rejected = 0
    for kind, crop, prompt in places[:_MAX_ASKS]:
        answer = await ask(prompt, sheet.crop(crop))
        value = normalize_level((answer or {}).get("level") or "")
        if value is None:
            rejected += 1
            continue
        levels.append(
            {
                "value": value,
                "kind": kind,
                "bbox_px": [round(v / factor, 1) for v in crop],
            }
        )
    return {
        "levels": levels,
        "values": sorted({item["value"] for item in levels}, key=float),
        "places_found": len(places),
        "places_asked": min(len(places), _MAX_ASKS),
        "rejected": rejected,
    }


async def _default_ask(prompt: str, image: Any) -> dict:
    from app.ai.cad_recognize.spec_fragments import _ask
    from app.ai.router import ai_router

    return await _ask(
        prompt,
        image,
        router=ai_router,
        confidential=True,
        num_predict=120,
        schema=LEVEL_AT_MARK_SCHEMA,
        timeout_seconds=60.0,
    )
