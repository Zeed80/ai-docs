"""Полки линий-выносок с номерами позиций (план, Ф2 `leader_label`, X5).

Номер позиции на сборочном чертеже и чертеже узла стоит на полке линии-выноски
(ГОСТ 2.109, 2.316): короткая горизонтальная черта, над ней номер, от одного
её конца наклонная тонкая линия к детали. Модель выписывала номера со всего
листа списком, и лист их не подтверждал: что номер действительно на полке,
а не размер или надпись разреза, никто не проверял.

Геометрия находит полки, номер по вырезу над полкой читает модель — тот же
приём, что у отметок уровня (`construction_levels`): выдумать номер ей
неоткуда, когда вырез показывает одну полку.

Полка отличается от размерной линии с числом над ней концами: у размерной
на обоих концах выносные (вертикальные) и стрелки, у полки один конец
свободен, а от другого уходит наклонная выноска.
"""

from __future__ import annotations

import math
from typing import Any

# Длина полки — доля длинной стороны листа: 8…30 мм на A4…A1.
_MIN_SHELF_SHARE = 0.008
_MAX_SHELF_SHARE = 0.06
# Надпись над полкой: доля чернил в полосе высотой в пол-полки.
_LABEL_INK = (0.015, 0.5)
# Наклон выноски от горизонтали и вертикали, градусы (ГОСТ 2.316: не
# параллельно размерным, выносным и штриховке).
_LEADER_ANGLE = (5.0, 80.0)
# Выноска — длинный прямой луч: покрытие чернилами на протяжении длины
# полки (окно растёт с расстоянием: наклон кольца квантован по 5°). Стрелка размера тоже наклонна у конца, но коротка.
_LEADER_REACH = 1.0
_LEADER_COVERAGE = 0.75
# Полка — тонкая линия: не толще этой доли основной линии листа.
_THIN_SHARE = 0.9
# Слитая с пологой выноской полка длиннее обычной: до этой доли листа.
_MAX_BENT_SHARE = 0.25

POSITION_PROMPT = (
    "На фрагменте — номер позиции на полке линии-выноски сборочного чертежа. "
    "Какое число стоит над полкой? Только число с листа. ОДНОЙ строкой JSON: "
    '{"position": 3} или {"position": null}. Только JSON.'
)
POSITION_SCHEMA = {"type": "object", "properties": {"position": {"type": ["integer", "null"]}}}


def position_shelves(gray: Any) -> list[dict[str, Any]]:
    """Полки выносок: ``{"shelf": [x0, x1, y], "label_box": [...], "leader": ...}``."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _lines

    gray = np.asarray(gray)
    ink = _ink(gray)
    reach = max(gray.shape)
    shortest = max(8, int(_MIN_SHELF_SHARE * reach))
    longest = _MAX_SHELF_SHARE * reach
    found = []
    lines = _lines(ink, shortest, axis=0)
    # Толщина основной линии — по длинным горизонталям листа (кромки детали,
    # рамка): самая толстая «линия» бывает слиянием наклонной с полкой.
    long_ones = sorted(
        _thickness(ink, line) for line in lines if line.end - line.start >= 3.0 * longest
    )
    main = long_ones[len(long_ones) // 2] if long_ones else 0.0
    for line in lines:
        if line.end - line.start > _MAX_BENT_SHARE * reach:
            continue
        if main and _thickness(ink, line) > _THIN_SHARE * main:
            continue
        # Пологая выноска (реальный «Лубрикатор»: 5…15°) сливается с полкой
        # в одну «горизонталь» — полка тогда ровный участок у одного конца,
        # а выноска — плавный уход строк на остальной длине.
        bent = _bent_shelf(ink, line)
        if bent is not None:
            start, end, y, leader_x, angle = bent
            if not shortest <= end - start <= longest:
                continue
            shelf = _Span(float(y), start, end)
            if _arrowheads(ink, shelf, y):
                continue
            label = _label_above(ink, start, end, y)
            free_x = start if leader_x == end else end
            outward = -1 if free_x == start else 1
            if label is None or not _free_end(ink, free_x, y, end - start, outward=outward):
                continue
            found.append(
                {
                    "shelf": [round(start, 1), round(end, 1), float(y)],
                    "label_box": label,
                    "leader": {"x": round(leader_x, 1), "angle": round(angle, 1)},
                }
            )
            continue
        length = line.end - line.start
        if length > longest:
            continue
        y = int(round(line.position))
        if _arrowheads(ink, line, y):
            continue
        label = _label_above(ink, line.start, line.end, y)
        if label is None:
            continue
        ends = [
            (line.start, _leader_angle(ink, line.start, y, length, toward=-1)),
            (line.end, _leader_angle(ink, line.end, y, length, toward=1)),
        ]
        leaders = [(x, angle) for x, angle in ends if angle is not None]
        free = [
            x
            for x, angle in ends
            if angle is None and _free_end(ink, x, y, length, outward=-1 if x == line.start else 1)
        ]
        if len(leaders) != 1 or len(free) != 1:
            continue
        found.append(
            {
                "shelf": [round(line.start, 1), round(line.end, 1), float(y)],
                "label_box": label,
                "leader": {"x": round(leaders[0][0], 1), "angle": round(leaders[0][1], 1)},
            }
        )
    return found


class _Span:
    """Участок горизонтали: то же, что линия `_lines` (положение, от, до)."""

    def __init__(self, position: float, start: float, end: float) -> None:
        self.position, self.start, self.end = position, start, end


def _bent_shelf(ink: Any, line: Any) -> tuple[float, float, int, float, float] | None:
    """Полка, слитая с пологой выноской: (от, до, строка, x излома, угол).

    Строка линии прослеживается по столбцам; ровный участок (±1 строка) у
    одного конца — полка, остальное обязано уходить монотонно и не меньше
    чем на 3 строки — выноска. Прямая без излома — не сюда (``None``).
    """
    import math

    rows = _row_profile(ink, line)
    if len(rows) < 8:
        return None
    for flat_at_start in (True, False):
        sequence = rows if flat_at_start else rows[::-1]
        anchor = sequence[0][1]
        flat = 0
        while flat < len(sequence) and abs(sequence[flat][1] - anchor) <= 1:
            flat += 1
        rest = sequence[flat:]
        if flat < 4 or len(rest) < 4:
            continue
        drift = [row - anchor for _x, row in rest]
        # Выноска — до первого разворота: в конце она входит в контур детали.
        sign = 1 if drift[min(len(drift) - 1, 3)] >= 0 else -1
        keep = 1
        while keep < len(drift) and sign * (drift[keep] - drift[keep - 1]) >= 0:
            keep += 1
        rest, drift = rest[:keep], drift[:keep]
        if len(rest) < 4 or abs(drift[-1]) < 3:
            continue
        flat_x = [x for x, _row in sequence[:flat]]
        junction = sequence[flat - 1][0]
        run = abs(rest[-1][0] - junction) or 1.0
        angle = math.degrees(math.atan2(abs(drift[-1]), run))
        if not _LEADER_ANGLE[0] - 2.0 <= angle <= _LEADER_ANGLE[1]:
            continue
        return (float(min(flat_x)), float(max(flat_x)), int(anchor), float(junction), angle)
    return None


def _row_profile(ink: Any, line: Any) -> list[tuple[int, int]]:
    """Строка чернил линии в каждом столбце — прослеживанием от соседнего."""
    # У слитой с выноской полки «положение» — центр тяжести компонента, а не
    # строка полки: прослеживание начинается с ближайших чернил первого
    # столбца в пределах четверти длины.
    row = int(round(line.position))
    reach = max(3, int(0.25 * (line.end - line.start)))
    first = int(line.start)
    candidates = [
        r
        for r in range(row - reach, row + reach + 1)
        if 0 <= r < ink.shape[0] and 0 <= first < ink.shape[1] and ink[r, first]
    ]
    if candidates:
        row = min(candidates, key=lambda value: abs(value - row))
    profile = []
    for column in range(int(line.start), int(line.end) + 1):
        if not 0 <= column < ink.shape[1]:
            continue
        near = [r for r in range(row - 2, row + 3) if 0 <= r < ink.shape[0] and ink[r, column]]
        if not near:
            continue
        row = min(near, key=lambda value: abs(value - row))
        profile.append((column, row))
    return profile


def _label_above(ink: Any, start: float, end: float, y: int) -> list[int] | None:
    """Надпись над полкой: чернила в полосе над ней, отделённые от линии."""
    import numpy as np

    height = max(6, int(0.6 * (end - start)))
    top = max(0, y - height)
    band = ink[top : max(0, y - 2), int(start) : int(end) + 1]
    if band.size == 0:
        return None
    share = float(band.mean())
    if not _LABEL_INK[0] <= share <= _LABEL_INK[1]:
        return None
    rows = np.flatnonzero(band.any(axis=1))
    columns = np.flatnonzero(band.any(axis=0))
    if len(rows) == 0 or len(columns) == 0:
        return None
    return [
        int(start) + int(columns[0]),
        top + int(rows[0]),
        int(start) + int(columns[-1]),
        top + int(rows[-1]),
    ]


def _leader_angle(ink: Any, x: float, y: int, length: float, *, toward: int) -> float | None:
    """Наклонная линия от конца полки: угол от горизонтали, если она есть.

    Кольцо радиусом в треть полки вокруг конца: чернила на нём под углом
    между горизонталью и вертикалью — выноска; строго вертикально — выносная
    размера, строго горизонтально — продолжение линии.
    """
    radius = max(6.0, length / 3.0)
    angles = []
    for step in range(72):
        angle = 2.0 * math.pi * step / 72
        column = int(round(x + radius * math.cos(angle)))
        row = int(round(y + radius * math.sin(angle)))
        if not (0 <= row < ink.shape[0] and 0 <= column < ink.shape[1]):
            continue
        if ink[max(0, row - 1) : row + 2, max(0, column - 1) : column + 2].any():
            degrees = math.degrees(angle) % 360.0
            # Сама полка уходит от конца внутрь — это не выноска.
            inward = 180.0 if toward > 0 else 0.0
            if min(abs(degrees - inward), 360.0 - abs(degrees - inward)) < 20.0:
                continue
            from_horizontal = min(degrees % 180.0, 180.0 - degrees % 180.0)
            if _LEADER_ANGLE[0] <= from_horizontal <= _LEADER_ANGLE[1]:
                angles.append(from_horizontal)
    if not angles:
        return None
    # Проверяется каждый найденный наклон: луч должен тянуться далеко.
    for angle in sorted(set(round(value) for value in angles)):
        for sign in (-1.0, 1.0):
            direction = math.radians(angle) * sign
            dx = math.cos(direction)
            if _ray_covered(ink, x, y, length, dx, math.sin(direction), toward):
                return float(angle)
    return None


def _ray_covered(
    ink: Any, x: float, y: int, length: float, dx: float, dy: float, toward: int
) -> bool:
    """Чернила вдоль луча от конца полки наружу на полторы длины полки."""
    for horizontal in (toward, -toward):
        hits = total = 0
        steps = int(_LEADER_REACH * length)
        for distance in range(int(length / 3.0), steps, 2):
            column = int(round(x + horizontal * abs(dx) * distance))
            row = int(round(y + dy * distance))
            if not (0 <= row < ink.shape[0] and 0 <= column < ink.shape[1]):
                break
            total += 1
            window = max(2, int(0.1 * distance))
            hits += bool(
                ink[max(0, row - window) : row + window + 1, max(0, column - 1) : column + 2].any()
            )
        if total >= 8 and hits / total >= _LEADER_COVERAGE:
            return True
    return False


def _arrowheads(ink: Any, line: Any, y: int) -> bool:
    """Стрелки размерной линии: залитый клин на линии или сразу за её концами.

    Кусок размерной «4» со стрелками снаружи и чужой наклонной рядом
    проходил за полку, и по вырезу модель прочла бы «4» номером позиции.
    """
    thickness = max(1.0, _thickness(ink, line))
    length = line.end - line.start
    for column in range(int(line.start - 0.3 * length), int(line.end + 0.3 * length) + 1):
        if not (0 <= column < ink.shape[1]) or not (0 <= y < ink.shape[0]) or not ink[y, column]:
            continue
        top = y
        while top > 0 and ink[top - 1, column] and y - top < 60:
            top -= 1
        bottom = y
        while bottom < ink.shape[0] - 1 and ink[bottom + 1, column] and bottom - y < 60:
            bottom += 1
        # Вверх от полки — надпись, она отделена зазором; клин стрелки
        # симметричен линии и толще неё в разы.
        # Сквозная вертикаль (линия вида, выносная) — не клин: она длинная.
        height = bottom - top + 1
        if (
            min(y - top, bottom - y) >= 1.5 * thickness
            and 3.0 * thickness <= height <= 8.0 * thickness
        ):
            return True
    return False


def _thickness(ink: Any, line: Any) -> float:
    """Толщина горизонтали: медиана вертикальных прогонов чернил по её длине."""
    y = int(round(line.position))
    runs = []
    for column in range(
        int(line.start), int(line.end) + 1, max(1, int((line.end - line.start) / 12))
    ):
        if not (0 <= y < ink.shape[0] and 0 <= column < ink.shape[1]) or not ink[y, column]:
            continue
        top = y
        while top > 0 and ink[top - 1, column] and y - top < 40:
            top -= 1
        bottom = y
        while bottom < ink.shape[0] - 1 and ink[bottom + 1, column] and bottom - y < 40:
            bottom += 1
        runs.append(bottom - top + 1)
    runs.sort()
    return float(runs[len(runs) // 2]) if runs else 0.0


def _free_end(ink: Any, x: float, y: int, length: float, *, outward: int) -> bool:
    """Свободный конец полки: вплотную за ним нет продолжения.

    Смотрится близкое кольцо — несколько толщин линии, а не доля полки:
    рамка листа в двух десятках пикселей от конца полки «Лубрикатора»
    делала каждый конец занятым. Верхняя половина не в счёт (над полкой
    надпись), сама полка — тоже.
    """
    radius = max(6.0, min(length / 4.0, 12.0))
    hits = 0
    for step in range(48):
        angle = 2.0 * math.pi * step / 48
        degrees = math.degrees(angle) % 360.0
        # Над полкой — надпись; назад — сама полка. Продолжение наружу, даже
        # почти горизонтальное, конец занимает: кусок пологой выноски, который
        # поиск горизонталей отрезал, «полкой» не становится.
        if 200.0 < degrees < 340.0:
            continue
        inward = 180.0 if outward > 0 else 0.0
        if min(abs(degrees - inward), 360.0 - abs(degrees - inward)) < 15.0:
            continue
        column = int(round(x + radius * math.cos(angle)))
        row = int(round(y + radius * math.sin(angle)))
        if 0 <= row < ink.shape[0] and 0 <= column < ink.shape[1]:
            # Окно 3×3: «ступеньки» сглаженной пологой линии смещены на строку.
            hits += bool(ink[max(0, row - 1) : row + 2, max(0, column - 1) : column + 2].any())
    return hits <= 2


def shelf_crop_box(item: dict[str, Any], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Вырез с номером над полкой: полка и надпись с полем."""
    x0, x1, y = item["shelf"]
    lx0, ly0, lx1, _ly1 = item["label_box"]
    pad = 0.3 * (x1 - x0)
    return (
        max(0, int(min(x0, lx0) - pad)),
        max(0, int(ly0 - pad)),
        min(size[0], int(max(x1, lx1) + pad)),
        min(size[1], int(y + pad)),
    )


async def read_sheet_positions(image_bytes: bytes, *, ask: Any = None) -> dict[str, Any]:
    """Номера позиций по полкам листа: ``{"positions": [...], "shelves": n}``."""
    import io

    import numpy as np
    from PIL import Image

    if ask is None:
        ask = _default_ask
    Image.MAX_IMAGE_PIXELS = None
    sheet = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    shelves = position_shelves(np.asarray(sheet.convert("L")))
    positions = []
    for item in shelves:
        crop = shelf_crop_box(item, sheet.size)
        answer = await ask(POSITION_PROMPT, sheet.crop(crop))
        value = (answer or {}).get("position")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 999:
            continue
        positions.append({"position": value, "bbox_px": list(crop)})
    return {"positions": positions, "shelves": len(shelves)}


async def _default_ask(prompt: str, image: Any) -> dict:
    from app.ai.cad_recognize.spec_fragments import _ask, _overview
    from app.ai.router import ai_router

    return await _ask(
        prompt,
        _overview(image),
        router=ai_router,
        confidential=True,
        num_predict=60,
        schema=POSITION_SCHEMA,
        timeout_seconds=60.0,
    )


def position_verdicts(
    positions: list[int], on_sheet: list[dict[str, Any]], rows_without_position: list[int]
) -> list[dict[str, Any]]:
    """Вердикты позициям сборки по полкам листа.

    Номер, выписанный моделью и прочитанный на полке, — подтверждён. Не
    найденный на полке — «не измеримо», не «опровергнут»: полку замер мог
    не найти. Номер с полки, которого модель не выписала, — находка, но
    только если он есть в спецификации: полка-обманка (кусок размерной со
    стрелкой) иначе превратила бы размер «4» в позицию.
    """
    by_number: dict[int, dict[str, Any]] = {}
    for item in on_sheet:
        by_number.setdefault(int(item["position"]), item)
    verdicts = []
    for number in positions:
        item = {
            "kind": "assembly_position",
            # Путь — по номеру позиции: панель подписывает «Позиция N» индексом
            # пути, и «Позиция 1» у позиции 13 сбивала бы оператора.
            "path": f"positions[{number - 1}]",
            "read": {"position": number},
        }
        found = by_number.get(number)
        if found is None:
            verdicts.append(
                {
                    **item,
                    "status": "unmeasurable",
                    "measured": {},
                    "reason": "полка с этим номером на листе не найдена",
                }
            )
            continue
        verdicts.append(
            {
                **item,
                "status": "confirmed",
                "measured": {"position": number},
                "evidence_bbox_px": found["bbox_px"],
                "reason": "номер прочитан на полке выноски",
            }
        )
    listed = set(positions)
    for number in sorted(set(by_number) - listed):
        if number not in rows_without_position:
            continue
        verdicts.append(
            {
                "kind": "assembly_position_found",
                "path": f"positions[{number - 1}]",
                "status": "confirmed",
                "read": {},
                "measured": {"position": number},
                "evidence_bbox_px": by_number[number]["bbox_px"],
                "reason": "позиция на полке листа, в чтении её не было; строка спецификации есть",
            }
        )
    return verdicts
