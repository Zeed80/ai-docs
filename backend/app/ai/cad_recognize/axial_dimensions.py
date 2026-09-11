"""Deterministic localisation of horizontal axial dimensions on CAD sheets.

The spec reader already knows the strings printed on the sheet, but a bag of
numbers is not a dimension chain.  This module keeps the missing spatial fact:
the OCR label bbox, the horizontal dimension line and the two extension lines
that terminate it.  Once the overall dimension is found, every observation can
be expressed relative to the left/right datum without asking a VLM to invent
coordinates.

Nothing here creates part geometry.  The observations are evidence supplied to
the reader and persisted in the process journal; incomplete or ambiguous maps
remain blockers.
"""

from __future__ import annotations

import math
import re
from typing import Any

_NUMBER_TOKEN = re.compile(r"[^0-9]*([0-9]+(?:[.,][0-9]+)?)[^0-9]*")

# Насколько отношение «мм на пиксель» одной линии может отличаться от медианы,
# чтобы считаться тем же масштабом. Разброс тут от округления подписей и
# толщины штриха, а не от разных видов: чужой вид ошибается кратно.
_SCALE_AGREEMENT_TOLERANCE = 0.12

# Измерение линии по чернилам рядом с подписью. Все три — в долях высоты
# текста: зазор, который считается разрывом одной линии (стрелка, просвет под
# числом), насколько далеко от подписи искать и какой пролёт уже не шум.
_INK_GAP = 1.2
_INK_REACH = 3.5
_INK_MIN_SPAN = 3.0
# Доля столбцов прогона, занятых чернилами, чтобы он считался линией.
_INK_MIN_FILL = 0.6
# Чернила относительно местного фона: не меньше этого перепада яркости и не
# меньше стольких сигм шума листа.
_INK_CONTRAST_MIN = 25
_INK_CONTRAST_NOISE = 4.0
# Выносная линия у размерной: насколько далеко смотреть вверх и вниз от строки и
# сколько чернил подряд нужно с КАЖДОЙ стороны, чтобы признать пересечение. Выход
# выносной за размерную линию по ГОСТ 2.307 — 1…5 мм, текст — 3,5…5 мм, отсюда
# доли высоты текста. Стрелка ниже порога: её крыло — треть её длины.
_WITNESS_REACH = 2.0
_WITNESS_MIN = 0.3
# И одна сторона обязана быть ДЛИННОЙ — выносная идёт к детали. Без этого резал
# задник залитой стрелки: он тоже пересекает строку и вверх, и вниз. Замерено
# на эталоне: выносная — вверх 23 px и вниз 441, задник стрелки — 11 и 13, а
# стрелка ближе к подписи и выигрывала; «15» мерилось как 94 px вместо 177.
_WITNESS_LONG = 1.2
_WITNESS_MIN_SPAN = 1.0


def _matches(value: float, candidates: list[float], relative: float = 0.005) -> bool:
    return any(
        abs(value - candidate) <= max(0.05, abs(candidate) * relative) for candidate in candidates
    )


def _nearest(value: float, candidates: list[float]) -> tuple[float | None, float]:
    if not candidates:
        return None, float("inf")
    candidate = min(candidates, key=lambda item: abs(item - value))
    error = abs(candidate - value) / max(abs(candidate), 1e-6)
    return candidate, error


def _plausible_ocr_correction(raw_text: str, candidate: float) -> bool:
    raw_digits = "".join(character for character in raw_text if character.isdigit())
    candidate_text = f"{candidate:g}".replace(".", "")
    if not raw_digits or not candidate_text:
        return False
    if candidate_text.endswith(raw_digits) or raw_digits.endswith(candidate_text):
        return abs(len(candidate_text) - len(raw_digits)) <= 1
    if len(candidate_text) != len(raw_digits):
        return False
    return sum(left != right for left, right in zip(raw_digits, candidate_text, strict=True)) <= 1


# Высота цифры, при которой Tesseract работает уверенно. Это свойство самого
# движка (его документация просит примерно 20-30 px на строчную букву), а не
# какого-то листа, поэтому число здесь и не подбиралось под чертёж.
_OCR_TARGET_TEXT_PX = 24.0
# Дальше растягивать бессмысленно: интерполяция не добавляет штрихов, а время
# растёт квадратично.
_OCR_MAX_UPSCALE = 3.0


def _read_numeric_tokens(image: Any) -> list[dict[str, Any]]:
    """Один проход OCR по листу как он есть."""
    import pytesseract
    from pytesseract import Output

    data = pytesseract.image_to_data(
        image,
        lang="rus+eng",
        config="--psm 11",
        output_type=Output.DICT,
    )
    tokens: list[dict[str, Any]] = []
    for index, raw in enumerate(data.get("text") or []):
        text = str(raw or "").strip()
        match = _NUMBER_TOKEN.fullmatch(text)
        if not match:
            continue
        try:
            value = float(match.group(1).replace(",", "."))
            confidence = float(data["conf"][index])
        except (TypeError, ValueError, KeyError):
            continue
        x = int(data["left"][index])
        y = int(data["top"][index])
        width = int(data["width"][index])
        height = int(data["height"][index])
        # Отсев в долях высоты самого текста, а не в пикселях: раньше пороги
        # 160×90 были ограничением конкретного разрешения и на крупном скане
        # выбрасывали обычные размерные числа.
        if not (0 < value <= 100_000 and width > 0 and height > 0):
            continue
        if width > 16 * height or height > 90 * max(1.0, image.height / 850.0):
            continue
        tokens.append(
            {
                "raw_text": text,
                "ocr_value_mm": value,
                "ocr_confidence": max(0.0, min(1.0, confidence / 100.0)),
                "label_bbox": [x, y, x + width, y + height],
            }
        )
    return tokens


def _ocr_numeric_tokens(image: Any) -> list[dict[str, Any]]:
    """Числа на листе, прочитанные в масштабе, в котором Tesseract их видит.

    Замерено на корпусе: на листах 1100×850 высота цифры — десять пикселей, и
    OCR находит четыре-семнадцать чисел там, где их два десятка. Это не
    свойство чертежей, а рабочий диапазон движка: ниже примерно двадцати
    пикселей он перестаёт различать штрихи.

    Поэтому масштаб подбирается по САМОМУ листу: первый проход измеряет высоту
    цифры, и если она мала — лист растягивается ровно настолько, чтобы попасть
    в рабочий диапазон, а координаты возвращаются в исходные. Ни одного числа,
    привязанного к разрешению: единственная константа — свойство Tesseract.
    """
    tokens = _read_numeric_tokens(image)
    unit = _text_unit(tokens) if tokens else 0.0
    if unit >= _OCR_TARGET_TEXT_PX:
        return tokens

    # Нечего измерить — пробуем разумную растяжку один раз: пустой результат
    # чаще означает «слишком мелко», чем «чисел нет».
    scale = _OCR_TARGET_TEXT_PX / unit if unit > 0 else 2.0
    scale = min(_OCR_MAX_UPSCALE, max(1.0, scale))
    if scale <= 1.0:
        return tokens

    from PIL import Image as _Image

    enlarged = image.resize((int(image.width * scale), int(image.height * scale)), _Image.LANCZOS)
    rescanned = _read_numeric_tokens(enlarged)
    if len(rescanned) <= len(tokens):
        # Растяжка не помогла — оставляем то, что прочитано в натуральном
        # масштабе. Меньше найденного не бывает лучше.
        return tokens
    for token in rescanned:
        token["label_bbox"] = [round(value / scale, 1) for value in token["label_bbox"]]
    return rescanned


def _hough_lines(image: Any) -> tuple[list[list[float]], list[list[float]]]:
    import cv2
    import numpy as np

    rgb = np.asarray(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges,
        1,
        math.pi / 180.0,
        threshold=25,
        minLineLength=20,
        maxLineGap=10,
    )
    horizontal: list[list[float]] = []
    vertical: list[list[float]] = []
    if lines is None:
        return horizontal, vertical
    for x1, y1, x2, y2 in lines[:, 0]:
        if abs(int(y2) - int(y1)) <= 2 and abs(int(x2) - int(x1)) >= 35:
            horizontal.append(
                [
                    float(min(x1, x2)),
                    float(y1 + y2) / 2.0,
                    float(max(x1, x2)),
                ]
            )
        if abs(int(x2) - int(x1)) <= 2 and abs(int(y2) - int(y1)) >= 20:
            vertical.append(
                [
                    float(x1 + x2) / 2.0,
                    float(min(y1, y2)),
                    float(max(y1, y2)),
                ]
            )
    return horizontal, vertical


def _text_unit(tokens: list[dict[str, Any]]) -> float:
    """Высота цифры на этом листе — естественная единица длины чертежа.

    Все пороги спаривания раньше стояли в АБСОЛЮТНЫХ пикселях: подпись искалась
    в полосе 3..35 px над линией, выносные — в окнах ±60/±20, минимальный
    пролёт 35. Числа были подобраны под лист, у которого высота цифры около
    десяти пикселей, и на любом другом разрешении переставали значить то, что
    задумано: на крупном скане подпись отстоит от линии дальше тридцати пяти
    пикселей, и линия просто не находится.

    ГОСТ 2.304/2.307 задаёт остальное ОТНОСИТЕЛЬНО высоты шрифта: и зазор между
    числом и линией, и выход выносных за размерную. Поэтому единица измерения
    здесь — сам текст, а не пиксель.
    """
    heights = sorted(
        float(token["label_bbox"][3] - token["label_bbox"][1])
        for token in tokens
        if token["label_bbox"][3] > token["label_bbox"][1]
    )
    if not heights:
        return 10.0
    return max(4.0, heights[len(heights) // 2])


def _ink_rows(image: Any) -> Any:
    """Чернила листа: тёмное на светлом, без разделения на деталь и рамку.

    Абсолютного порога мало. На фото листа (тонированная бумага ~228, свет
    неравномерный, размытие и ужатие до 1000–1700 px) тонкие размерные и
    выносные линии светлеют до ~190, и порог 160 оставлял от размера одни
    стрелки: замер находил 31 % размеров против 97 % на скане той же цифры.
    Поэтому чернила — ещё и всё, что заметно темнее МЕСТНОГО фона, а
    «заметно» — в единицах оценённого шума, чтобы зерно не стало линией.
    """
    import numpy as np

    gray = np.asarray(image.convert("L"))
    dark = gray < 160
    try:
        import cv2
    except ImportError:  # pragma: no cover — без OpenCV остаётся прежний порог
        return dark
    height, width = gray.shape
    size = max(15, min(height, width) // 60) | 1
    background = cv2.blur(cv2.dilate(gray, np.ones((size, size), np.uint8)), (size, size))
    contrast = background.astype(np.int16) - gray.astype(np.int16)
    # Фон — максимум по окрестности, и на зашумлённом листе он выше истинного
    # на пару сигм: перепад отсчитывается от его медианы, а не от нуля. Без
    # этого шум σ18 давал ложные чернила, и верных замеров стало 90 % вместо 100.
    level = float(np.median(contrast))
    spread = float(np.median(np.abs(contrast - level)))
    step = max(_INK_CONTRAST_MIN, _INK_CONTRAST_NOISE * 1.4826 * spread)
    return dark | (contrast - level > step)


def _span_from_ink(ink: Any, bbox: list[float], unit: float) -> list[float] | None:
    """Измерить размерную линию РЯДОМ С ПОДПИСЬЮ, а не искать её на листе.

    Hough находит размерную линию обрывками: её рвут стрелки, пересечения с
    выносными, разрыв под числом и слабый контраст скана. Замерено на
    `z4-r4.jpg`: линия габарита 195 существует на y=60, а Hough отдаёт
    467..513 и 972..1036 — подпись стоит на x=576, ровно между кусками, и
    пара не складывается. Склейка коллинеарных отрезков это чинила, но
    ломала `detal_126`: склеенные пролёты вытесняли верные пары.

    Здесь задача другая и куда более узкая. Отличать штрихи детали от рамки
    листа не нужно: VLM уже сказал, ГДЕ стоит число, и мы смотрим только в его
    окрестность. Берётся ближайшая к подписи строка, в которой чернила идут
    прогоном, накрывающим подпись, — с допуском на разрывы. Ближайшая, а не
    самая длинная: над одной длинной линией стоит несколько чисел, и по
    длине все они получали бы её пролёт.

    Это тот самый мост «геометрия измерением, надписи чтением», а не оживление
    отдельной трассировки: измерение существует только затем, чтобы дать
    прочитанному числу длину.
    """

    x0, y0, x1, y1 = (float(value) for value in bbox)
    height, _width = ink.shape
    centre = (x0 + x1) / 2.0
    best: tuple[tuple[float, float], int, list[float]] | None = None
    top = max(0, int(y0 - _INK_REACH * unit))
    bottom = min(height, int(y1 + _INK_REACH * unit))
    for y in range(top, bottom):
        if int(y0) <= y <= int(y1):
            continue  # строки самой подписи
        run = _line_run(ink, y, centre, unit)
        if run is None:
            continue
        key = (min(abs(y - y0), abs(y - y1)), -(run[1] - run[0]))
        if best is None or key < best[0]:
            best = (key, y, [run[0], float(y), run[1], float(y)])
    if best is None:
        return None
    # Ближайшая к подписи подходящая строка — край полосы «линия + стрелки»,
    # а не сама линия: на размытом листе это строка внутри пятна стрелок, где
    # к ним прилипает низ цифры, и штрихи цифры засчитывались выносными у
    # самой подписи — отсечение сдавалось. Стрелки симметричны относительно
    # линии, поэтому линия — середина полосы.
    _key, first, line = best
    last = first
    step = 1 if first > y1 else -1
    while 0 <= last + step < height and _line_run(ink, last + step, centre, unit) is not None:
        last += step
    middle = (first + last) // 2 if step > 0 else (first + last + 1) // 2
    run = _line_run(ink, middle, centre, unit)
    if run is not None:
        line = [run[0], float(middle), run[1], float(middle)]
    return _cut_at_witness_lines(ink, line, centre, unit, label=(x0, y0, x1, y1))


def _line_run(ink: Any, y: int, centre: float, unit: float) -> tuple[float, float] | None:
    """Прогон чернил строки ``y`` под подписью с центром ``centre``, если это линия."""
    import numpy as np

    columns = np.flatnonzero(ink[y])
    if columns.size < 2:
        return None
    max_gap = max(4, int(_INK_GAP * unit))
    minimum = _INK_MIN_SPAN * unit
    breaks = np.flatnonzero(np.diff(columns) > max_gap)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [columns.size - 1]))
    best: tuple[float, float] | None = None
    for start, end in zip(starts, ends, strict=True):
        left, right = float(columns[start]), float(columns[end])
        if right - left < minimum:
            continue
        if not (left - unit <= centre <= right + unit):
            continue
        # Линия — это строка, почти сплошь залитая чернилами. Без этого
        # условия на размытом листе «линией» становилась строка, где стоят
        # только две выносные и низ цифры: промежутки между ними меньше
        # допустимого разрыва, и прогон склеивался.
        if (end - start + 1) < _INK_MIN_FILL * (right - left + 1):
            continue
        if best is None or right - left > best[1] - best[0]:
            best = (left, right)
    return best


def _cut_at_witness_lines(
    ink: Any,
    line: list[float],
    centre: float,
    unit: float,
    *,
    label: tuple[float, float, float, float] | None = None,
) -> list[float]:
    """Обрезать прогон по выносным линиям, пересекающим его рядом с подписью.

    Звенья размерной цепочки лежат на одной прямой и касаются друг друга, и
    чернила идут одним прогоном через всю цепочку. Замерено на корпусе с
    эталоном: у вала shaft-0 размеры 142, 177, 295 и 213 px все измерились как
    829 — длина всей цепочки; так было в 66 случаях из 164 горизонтальных.

    Конец размерной линии задаёт выносная: она пересекает строку и уходит
    вверх (на выход за размерную линию) и вниз (к детали). Такие пересечения
    режут прогон, и берётся отрезок между ближайшими к подписи. Выносная —
    граница размера даже точнее стрелки. Если пересечений с одной стороны
    нет, та сторона остаётся концом прогона, как было.
    """
    import numpy as np

    left, row, right = int(line[0]), int(line[1]), int(line[2])
    height = ink.shape[0]
    # Сколько чернил подряд над и под строкой в каждом столбце прогона.
    reach = max(3, int(round(_WITNESS_REACH * unit)))
    top = max(0, row - reach)
    bottom = min(height, row + reach + 1)
    band = ink[top:bottom, left : right + 1]
    if label is not None:
        # Сама подпись — не выносная. На размытом листе низ цифры сливается
        # со стрелками, и столбцы под подписью получали длинную сторону вверх
        # через цифру: штрихи «8» резали размер 47 px до 5. Настоящая выносная
        # через подпись не идёт, а её выход за линию лежит в зазоре под ней.
        band = band.copy()
        lx0, ly0, lx1, ly1 = (int(round(value)) for value in label)
        rows = slice(max(0, ly0 - top), max(0, min(bottom, ly1 + 1) - top))
        cols = slice(max(0, lx0 - left), max(0, min(right + 1, lx1 + 1) - left))
        band[rows, cols] = False
    centre_row = row - top
    above = band[:centre_row][::-1]
    below = band[centre_row + 1 :]
    up = np.cumprod(above, axis=0).sum(axis=0) if above.size else np.zeros(band.shape[1])
    down = np.cumprod(below, axis=0).sum(axis=0) if below.size else np.zeros(band.shape[1])
    need = max(2, int(round(_WITNESS_MIN * unit)))
    long = max(need + 1, int(round(_WITNESS_LONG * unit)))
    crossing = (
        np.flatnonzero((np.minimum(up, down) >= need) & (np.maximum(up, down) >= long)) + left
    )
    if crossing.size == 0:
        return line
    # Выносная толщиной в несколько столбцов — один конец, по её середине:
    # внутренняя кромка давала размер короче на толщину линии.
    runs = np.split(crossing, np.flatnonzero(np.diff(crossing) > 1) + 1)
    middles = np.array([(run[0] + run[-1]) / 2.0 for run in runs])
    before = middles[middles < centre]
    after = middles[middles > centre]
    new_left = float(before.max()) if before.size else line[0]
    new_right = float(after.min()) if after.size else line[2]
    # Порог здесь ниже, чем у самого прогона: пересечение уже подтверждено
    # длинной стороной, а цифра подписи строку размерной линии не пересекает.
    # С порогом прогона (3 высоты текста) короткий размер 6 мм — 71 px при
    # цифре 31 px — молча мерился всей линией вместе с выносом за стрелки.
    if new_right - new_left < _WITNESS_MIN_SPAN * unit:
        return line
    return [new_left, line[1], new_right, line[3]]


def _pair_tokens_with_lines(
    tokens: list[dict[str, Any]],
    horizontal: list[list[float]],
    vertical: list[list[float]],
    *,
    ink: Any = None,
) -> list[dict[str, Any]]:
    """Связать число с его размерной линией, где бы оно ни стояло.

    Раньше число искалось строго НАД линией. Это основная посадка по
    ГОСТ 2.307, но не единственная: когда между выносными тесно, число выносят
    вбок или под линию, а на разрезах и видах снизу так делают и без тесноты.
    Замерено на корпусе: у `welded_bracket.png` девять подписей из
    одиннадцати стоят ПОД линией, у `bearing_housing_section.png` — пять из
    шести. Обе стадии честно отдавали «размерные линии не связаны с выносками»,
    хотя линии и числа на листе были.
    """
    # Окна — исходные абсолютные, с пропорциональным ПОЛОМ, а не заменой.
    # Проверено замером: зазор между числом и его размерной линией с высотой
    # текста не растёт. На `detal_126.png` высота цифры тридцать пикселей, а
    # зазор укладывается в те же тридцать пять, что и на листах с высотой
    # десять; заменив окна на кратные высоте, я утроил допуск по зазору и
    # утроил порог пролёта — и потерял 78, 99, 150 и 270, которые до того
    # находились. Множители ниже включаются только там, где текст ЗАМЕТНО
    # крупнее, и на сегодняшнем корпусе ничего не меняют.
    unit = _text_unit(tokens)
    gap_min, gap_max = 3.0, max(35.0, 1.2 * unit)
    side_margin = max(15.0, 0.8 * unit)
    outward, inward = max(60.0, 2.0 * unit), max(20.0, 0.8 * unit)
    reach = max(15.0, 0.5 * unit)
    min_span = max(35.0, 1.2 * unit)

    paired: list[dict[str, Any]] = []
    for token in tokens:
        x0, y0, x1, y1 = token["label_bbox"]
        center_x = (x0 + x1) / 2.0
        above = [
            (line, line[1] - y1)
            for line in horizontal
            if gap_min <= line[1] - y1 <= gap_max
            and line[0] - side_margin <= center_x <= line[2] + side_margin
        ]
        # Число ПОД линией — та же связь, но посадка не основная, поэтому она
        # рассматривается только когда основная связи не дала. Иначе чужая
        # линия, случайно оказавшаяся ближе сверху, перебивает свою: замерено
        # на `detal_126.png`, где так терялись 78, 99, 150 и 270.
        below = [
            (line, y0 - line[1])
            for line in horizontal
            if gap_min <= y0 - line[1] <= gap_max
            and line[0] - side_margin <= center_x <= line[2] + side_margin
        ]
        paired_line = None
        for group in (above, below):
            if paired_line is not None:
                break
            # Ближайшая линия, а среди равноудалённых — самая длинная: короткий
            # штрих рядом с числом чаще выноска, чем размерная линия.
            for line, _distance in sorted(
                group, key=lambda item: (item[1], -(item[0][2] - item[0][0]))
            ):
                line_x0, line_y, line_x1 = line
                left_extensions = [
                    item
                    for item in vertical
                    if line_x0 - outward <= item[0] <= line_x0 + inward
                    and item[1] - reach <= line_y <= item[2] + reach
                ]
                right_extensions = [
                    item
                    for item in vertical
                    if line_x1 - inward <= item[0] <= line_x1 + outward
                    and item[1] - reach <= line_y <= item[2] + reach
                ]
                if left_extensions and right_extensions:
                    paired_line = (line, left_extensions, right_extensions)
                    break
        if paired_line is None:
            continue
        (line_x0, line_y, line_x1), left_extensions, right_extensions = paired_line
        left = min(left_extensions, key=lambda item: abs(item[0] - line_x0))
        right = min(right_extensions, key=lambda item: abs(item[0] - line_x1))
        if right[0] - left[0] < min_span:
            continue
        paired.append(
            {
                **token,
                "line": [round(left[0], 1), round(line_y, 1), round(right[0], 1), round(line_y, 1)],
                "span_px": round(right[0] - left[0], 1),
            }
        )
    if ink is not None:
        paired.extend(_ink_paired(tokens, paired, ink, unit, min_span))
    return paired


def _ink_paired(
    tokens: list[dict[str, Any]],
    paired: list[dict[str, Any]],
    ink: Any,
    unit: float,
    min_span: float,
) -> list[dict[str, Any]]:
    """Второй заход по чернилам — только для подписей, которым Hough не помог.

    Добавляется, а не заменяет: то, что уже спарено векторно, надёжнее, и
    трогать его незачем. Так же и поведение на листах, где Hough справляется,
    остаётся прежним.
    """
    already = {id(item) for item in paired}
    extra: list[dict[str, Any]] = []
    for token in tokens:
        if any(item.get("label_bbox") == token["label_bbox"] for item in paired):
            continue
        if id(token) in already:
            continue
        line = _span_from_ink(ink, token["label_bbox"], unit)
        if line is None or line[2] - line[0] < min_span:
            continue
        extra.append(
            {
                **token,
                "line": [
                    round(line[0], 1),
                    round(line[1], 1),
                    round(line[2], 1),
                    round(line[3], 1),
                ],
                "span_px": round(line[2] - line[0], 1),
                "line_source": "ink",
            }
        )
    return extra


def _deduplicate(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[int, int, int], dict[str, Any]] = {}
    for item in observations:
        line = item["line"]
        key = (round(line[0] / 3), round(line[2] / 3), round(line[1] / 3))
        current = best.get(key)
        if current is None or item["ocr_confidence"] > current["ocr_confidence"]:
            best[key] = item
    return list(best.values())


def _merge_callout_tokens(
    tokens: list[dict[str, Any]],
    callout_entries: list[tuple[float, list[float] | None]] | None,
) -> list[dict[str, Any]]:
    """Добавить числа, прочитанные моделью, там где она вернула координаты.

    Замерено на корпусе: на листах, где высота цифры десять пикселей,
    tesseract читает «12885», «95006», «Ва0.8», «АДЗ1» — и геометрическая
    проверка честно их отбрасывает, оставляя стадию пустой на чертежах,
    которые модель читает верно (у `flange_detail.png` она взяла все пять
    фактов эталона). Разные варианты подготовки изображения — растяжка,
    Otsu, размытие с порогом — проверены и не помогают: движок эти листы
    просто не читает.

    Числа из текстового слоя приходят уже отобранными (`_callout_entries`):
    диаметры, шероховатость, углы и количества отверстий отсеяны там же, где
    и для остального конвейера, — второго расходящегося отбора здесь нет.
    Уверенность им ставится высокая, но не единица: геометрическая сверка с
    пролётом линии остаётся обязательной, потому что связь «число ↔ линия»
    по-прежнему выводится, а не прочитана.

    Когда текстовый слой координат не вернул (`bbox` пуст — так делает
    документная OCR-модель без grounding), список пуст, и всё остаётся ровно
    как было. Это единственная причина, по которой стадия не чинится
    полностью: связывать нечего.
    """
    if not callout_entries:
        return tokens
    merged = list(tokens)
    for value, bbox in callout_entries:
        if not bbox or value <= 0:
            continue
        # Токен tesseract на том же месте вытесняется: число модели вернее.
        merged = [item for item in merged if not _boxes_overlap(item["label_bbox"], bbox)]
        merged.append(
            {
                "raw_text": f"{value:g}",
                "ocr_value_mm": float(value),
                "ocr_confidence": 0.95,
                "label_bbox": [float(item) for item in bbox],
                "value_from": "callout",
            }
        )
    return merged


def _boxes_overlap(left: list[float], right: list[float]) -> bool:
    """Пересекаются ли рамки хотя бы наполовину меньшей из них."""
    x0 = max(float(left[0]), float(right[0]))
    y0 = max(float(left[1]), float(right[1]))
    x1 = min(float(left[2]), float(right[2]))
    y1 = min(float(left[3]), float(right[3]))
    if x1 <= x0 or y1 <= y0:
        return False
    intersection = (x1 - x0) * (y1 - y0)
    areas = [
        max(1e-6, (float(box[2]) - float(box[0])) * (float(box[3]) - float(box[1])))
        for box in (left, right)
    ]
    return intersection >= 0.5 * min(areas)


def _calibration_base(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Линия из самой многочисленной группы, согласной по масштабу.

    Раньше базой была просто САМАЯ ШИРОКАЯ линия, а число на ней принималось
    на веру. Одна ошибка локализации — и весь лист получал чужой масштаб.
    Живой `shaft_detail.png`: за общий габарит бралось 3.2 мм, то есть Ra,
    затесавшаяся в список линейных выносок, — вал длиной в сотни миллиметров
    калибровался по значку чистоты поверхности.

    Инвариант: на одном виде и в одном масштабе длина размерной линии
    пропорциональна числу на ней. Но брать МЕДИАНУ отношений нельзя — верных
    наблюдений не обязано быть большинство. На `detal_126.png` верный масштаб
    0.339 мм/px разделяют пять наблюдений (470, 270, 240, 99, 78), а неверных
    спариваний больше, и медиана уезжает к ним: база получалась 20 мм при
    габарите листа 470.

    Поэтому берётся не середина, а самая многочисленная согласованная группа,
    при равенстве — та, чьи линии длиннее: длинная размерная линия — более
    сильное свидетельство, чем короткий штрих. База — самая широкая линия
    внутри группы-победителя.
    """
    usable = [
        item
        for item in candidates
        if float(item.get("span_px") or 0.0) > 0 and float(item.get("ocr_value_mm") or 0.0) > 0
    ]
    if len(usable) < 2:
        return max(candidates, key=lambda item: item["span_px"])

    best: tuple[int, float] | None = None
    winners: list[dict[str, Any]] = []
    for item in usable:
        ratio = float(item["ocr_value_mm"]) / float(item["span_px"])
        group = [
            other
            for other in usable
            if abs(float(other["ocr_value_mm"]) / float(other["span_px"]) - ratio)
            <= ratio * _SCALE_AGREEMENT_TOLERANCE
        ]
        score = (len(group), sum(float(other["span_px"]) for other in group))
        if best is None or score > best:
            best = score
            winners = group
    return max(winners or usable, key=lambda item: item["span_px"])


def localize_axial_dimensions(
    image: Any,
    known_linear_values: list[float],
    *,
    callout_entries: list[tuple[float, list[float] | None]] | None = None,
) -> dict[str, Any]:
    """Return datum-relative dimension observations with pixel evidence.

    The overall line is the widest OCR-labelled line whose value already
    occurs in the independently read callout list.  Its ratio calibrates only
    the dimension-line spans; it is never used to measure the part silhouette.
    OCR slips such as ``50`` for ``150`` may then be corrected only when one
    unique known callout agrees with that calibrated span within 4%.
    """
    known = sorted({round(float(value), 3) for value in known_linear_values if value > 0})
    try:
        tokens = _merge_callout_tokens(_ocr_numeric_tokens(image), callout_entries)
        horizontal, vertical = _hough_lines(image)
        ink = _ink_rows(image)
    except Exception as exc:  # noqa: BLE001 — localisation is an optional reader aid
        return {
            "status": "unavailable",
            "observations": [],
            "blockers": [f"локализатор размерных линий недоступен: {type(exc).__name__}"],
        }
    paired = _deduplicate(_pair_tokens_with_lines(tokens, horizontal, vertical, ink=ink))
    overall_candidates = [item for item in paired if _matches(item["ocr_value_mm"], known)]
    # The callout VLM may miss a number that Tesseract has already tied to a
    # real dimension line. In that case the widest paired line is the only
    # admissible overall candidate: it spans the two extreme datum extensions,
    # so using its own printed label is observation, not silhouette measuring.
    if not overall_candidates:
        overall_candidates = [
            item for item in paired if float(item.get("ocr_confidence") or 0.0) >= 0.45
        ]
    # Габаритом не может быть число, которое сам лист многократно превосходит.
    # Живой `shaft_detail.png`: единственным кандидатом оказалось 3.2 — Ra,
    # затесавшаяся в линейные выноски, — и стадия сначала калибровалась по
    # ней, а потом сама же себя отвергала, называя причиной 3.2, будто это был
    # рассмотренный вариант. Отбор и объяснение должны совпадать: заведомо
    # непригодное отсекается ДО выбора базы, и тогда честный ответ —
    # «подходящей линии нет», а не разбор негодного кандидата.
    largest_known = max(known) if known else 0.0
    if largest_known > 0:
        plausible = [
            item
            for item in overall_candidates
            if float(item["ocr_value_mm"]) * _OVERALL_UNDERSHOOT >= largest_known
        ]
        if plausible:
            overall_candidates = plausible
        else:
            return {
                "status": "unresolved",
                "observations": [],
                "blockers": [
                    "не найдена размерная линия общего осевого габарита: "
                    f"лист несёт {largest_known:g} мм, а ни одна связанная с "
                    "выноской линия такого размера не подписана"
                ],
            }
    if not overall_candidates:
        return {
            "status": "unresolved",
            "observations": [],
            "blockers": ["не найдена размерная линия общего осевого габарита"],
        }
    overall = _calibration_base(overall_candidates)
    overall_value = float(overall["ocr_value_mm"])
    overall_span = float(overall["span_px"])
    if overall_value <= 0 or overall_span <= 0:
        return {
            "status": "unresolved",
            "observations": [],
            "blockers": ["общий осевой габарит не задаёт масштаб размерных линий"],
        }
    mm_per_px = overall_value / overall_span
    datum_left, _line_y, datum_right, _ = overall["line"]
    datum_tolerance = max(12.0, overall_span * 0.025)

    accepted: list[dict[str, Any]] = []
    for item in paired:
        line_left, line_y, line_right, _ = item["line"]
        if line_left < datum_left - datum_tolerance or line_right > datum_right + datum_tolerance:
            continue
        measured = item["span_px"] * mm_per_px
        snapped, snap_error = _nearest(measured, known)
        ocr_value = float(item["ocr_value_mm"])
        value = ocr_value
        corrected = False
        value_source = "callout"
        ocr_span_error = abs(measured - ocr_value) / max(ocr_value, 1e-6)
        if not _matches(ocr_value, known) and ocr_span_error <= 0.02:
            # The printed token and its independently calibrated dimension
            # line agree. Prefer that direct pair over a nearby VLM candidate:
            # on detal_126 the true ``35`` otherwise snapped to an unrelated
            # ``36`` from the angular note ``36°×2``.
            value_source = "dimension_line_ocr"
        elif (
            snapped is not None
            and snap_error <= 0.04
            and (
                _matches(ocr_value, [snapped])
                or _plausible_ocr_correction(item["raw_text"], snapped)
            )
        ):
            value = snapped
            corrected = not _matches(ocr_value, [snapped])
            value_source = "callout_span_crosscheck"
        elif not _matches(ocr_value, known):
            # A missing leading digit (the real sheet's OCR ``50`` for
            # ``150``) is recoverable from the independently calibrated span,
            # but only when the printed token differs by that narrow OCR edit.
            geometric = float(round(measured))
            geometric_error = abs(measured - geometric) / max(geometric, 1e-6)
            raw_digits = "".join(character for character in item["raw_text"] if character.isdigit())
            geometric_digits = f"{geometric:g}".replace(".", "")
            if (
                geometric > 0
                and geometric_error <= 0.04
                and len(geometric_digits) == len(raw_digits) + 1
                and _plausible_ocr_correction(item["raw_text"], geometric)
            ):
                value = geometric
                corrected = True
                value_source = "dimension_span_ocr_correction"
            else:
                # The token itself is still source evidence: OCR bbox + a
                # dimension line with two extension lines. Keep it only if its
                # value agrees geometrically below; the confidence gate rejects
                # labels whose span says something else.
                value_source = "dimension_line_ocr"

        left_aligned = abs(line_left - datum_left) <= datum_tolerance
        right_aligned = abs(line_right - datum_right) <= datum_tolerance
        if left_aligned and right_aligned:
            relation = "overall"
            station = overall_value
        elif left_aligned:
            relation = "from_left_datum"
            station = value
        elif right_aligned:
            relation = "from_right_datum"
            station = overall_value - value
        else:
            relation = "local_interval"
            station = None
        mismatch = abs(measured - value) / max(value, 1e-6)
        confidence = min(float(item["ocr_confidence"]), max(0.0, 1.0 - mismatch))
        if confidence < 0.45:
            continue
        if confidence < 0.6:
            station = None
        accepted.append(
            {
                "id": "",
                "raw_text": item["raw_text"],
                "value_mm": round(value, 3),
                "ocr_value_mm": round(ocr_value, 3),
                "ocr_corrected": corrected,
                "value_source": value_source,
                "relation": relation,
                "station_from_left_mm": round(station, 3) if station is not None else None,
                "label_bbox": item["label_bbox"],
                "dimension_line": item["line"],
                "span_px": item["span_px"],
                "span_check_mm": round(measured, 2),
                "confidence": round(confidence, 3),
            }
        )
    accepted.sort(key=lambda item: (item["dimension_line"][1], item["dimension_line"][0]))
    for index, item in enumerate(accepted, start=1):
        item["id"] = f"axial-dim-{index}"

    blockers = list(_calibration_blockers(overall_value, known, accepted))
    if not accepted:
        blockers.append("осевые размерные линии не связаны с выносками")
    return {
        # Уверенно неверная калибровка хуже её отсутствия: масштаб уходит
        # дальше по конвейеру и подмешивается в проверки, а стадия помечена
        # как успешная. Живой прогон вернул overall_mm 45 для вала, у которого
        # собственная цепочка чертежа доходит до 195: локализатор поймал
        # габарит выносного элемента (Б-Б 5:1) и принял его за общий.
        "status": "ok" if accepted and not blockers else "unresolved",
        "overall_mm": round(overall_value, 3),
        "datum_line": [datum_left, datum_right],
        "mm_per_px": round(mm_per_px, 6),
        "observations": accepted,
        "blockers": blockers,
        # На скольких наблюдениях держится масштаб. Проверка невязки
        # сравнивает подписи с пролётами, поэтому на ЕДИНСТВЕННОМ наблюдении
        # ей нечего сравнивать: калибровка проверена сама собой и выглядит
        # ровно так же уверенно, как подтверждённая четырнадцатью. Отвергать
        # такую нельзя — на `shaft_detail.png` именно она даёт верные 840 мм,
        # совпадающие с эталоном, — но и выдавать её за проверенную тоже.
        "scale_support": _scale_support(accepted)["agreeing"],
        "scale_verified": _scale_support(accepted)["independent"] >= 1,
    }


# Напечатанное число и независимо измеренная длина линии, расходящиеся
# сильнее этой доли, — не подтверждение масштаба.
_SPAN_MISMATCH_TOLERANCE = 0.08
# Во сколько раз выноска с листа должна превышать «общий» габарит, чтобы стало
# ясно: за общий приняли не тот размер.
_OVERALL_UNDERSHOOT = 1.5


def _scale_support(accepted: list[dict[str, Any]]) -> dict[str, Any]:
    """Сколько размерных линий подтверждает выбранный масштаб и есть ли соперник.

    Раньше масштаб отвергался, если с ним расходилось больше половины
    наблюдений. Так судилась вся популяция, включая неверные спаривания:
    число шероховатости «0,80», прочитанное как «80» над контуром, или замер,
    взявший соседнюю линию. На `z4-r4` верный габарит 195 (его независимо
    подтвердила диаметральная стадия) отвергался из-за «6 из 10».

    Неверный масштаб выглядит иначе: расхождения СОГЛАСОВАНЫ между собой —
    все линии длиннее своих чисел в одно и то же число раз. Случайные ошибки
    спаривания разбросаны. Поэтому считается группа-соперник: наблюдения,
    которые согласно указывают один и тот же другой масштаб.
    """
    ratios = [
        (
            float(item["span_check_mm"]) / max(float(item["value_mm"]), 1e-6),
            item.get("relation") == "overall",
        )
        for item in accepted
        if float(item.get("value_mm") or 0.0) > 0
    ]
    agreeing = [base for ratio, base in ratios if abs(ratio - 1.0) <= _SPAN_MISMATCH_TOLERANCE]
    off = [ratio for ratio, _base in ratios if abs(ratio - 1.0) > _SPAN_MISMATCH_TOLERANCE]
    rival, rival_ratio = 0, 1.0
    for ratio in off:
        group = [other for other in off if abs(other - ratio) <= ratio * _SPAN_MISMATCH_TOLERANCE]
        if len(group) > rival:
            rival, rival_ratio = len(group), sum(group) / len(group)
    return {
        "agreeing": len(agreeing),
        "independent": sum(1 for base in agreeing if not base),
        "rival": rival,
        "rival_ratio": rival_ratio,
    }


def _calibration_blockers(
    overall_value: float, known: list[float], accepted: list[dict[str, Any]]
) -> list[str]:
    """Признаки того, что за общий габарит принят не тот размер."""
    blockers: list[str] = []
    larger = [value for value in known if value > overall_value * _OVERALL_UNDERSHOOT]
    if larger:
        blockers.append(
            f"общий габарит принят за {overall_value:g} мм, но лист несёт "
            f"больший размер {max(larger):g} мм — вероятно, измерен выносной элемент"
        )
    support = _scale_support(accepted)
    if support["rival"] >= 2 and support["rival"] > support["agreeing"]:
        blockers.append(
            f"длины размерных линий не сходятся с числами на них: {support['rival']} "
            f"согласованно указывают другой масштаб (×{support['rival_ratio']:.2f}), "
            f"выбранный подтверждают {support['agreeing']} — масштаб определён неверно"
        )
    return blockers
