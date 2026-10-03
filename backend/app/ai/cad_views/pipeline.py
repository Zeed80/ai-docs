"""Сквозной конвейер метода `views`: лист → тело вращения (первая стратегия D).

1. Роли областей листа (этап A, `sheet_reading`).
2. Надписи — одним вопросом модели, смысл — разбором ЕСКД (`labels`).
3. Главное изображение: увеличение до рабочей толщины линии; ось симметрии
   (горизонтальная или вертикальная — вертикальная поворачивается).
4. Профиль: по материалу разреза (`section_material`) или по силуэту вида.
5. Масштабы: по радиусу — надписи Ø, вдоль оси — габарит.
6. Элементы по видам той же детали в проекционной связи (`view_features`).
7. Тело — операциями ядра (`revolve_body` + элементы по размещению).

Другие стратегии D (выдавливание плана, корпуса) — следующие шаги; деталь,
которая не тело вращения, честно возвращается с причиной.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

LABELS_PROMPT = (
    "Выпиши с чертежа ВСЕ размерные надписи и обозначения ровно так, как они "
    "написаны (с Ø, R, M, допусками, «гл.», «N отв.», фасками «1×45°», углами). "
    "Не пересчитывай и не придумывай. Только надписи у изображений детали: "
    "содержимое таблиц (параметры зубчатого венца, спецификация), основной "
    "надписи (штампа) и технических требований НЕ выписывай — это не размеры. "
    'Ответ — ОДНОЙ строкой JSON: {"labels": ["...", "..."]}'
)
LABELS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"labels": {"type": "array", "items": {"type": "string"}}},
    "required": ["labels"],
}
# Рабочая толщина основной линии для замеров (px).
_WORK_LINE_PX = 7.0


@dataclass
class ViewsResult:
    ok: bool
    reason: str = ""
    candidate: dict[str, Any] | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    features: list[dict[str, Any]] = field(default_factory=list)
    scales: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # E1: надписи листа, найденные / не найденные на построенном теле.
    coverage: dict[str, Any] = field(default_factory=dict)


def _line_px(gray: Any) -> float:
    import numpy as np

    from app.ai.cad_views.section_material import ink_mask

    ink = ink_mask(gray, 3.0)
    runs: list[int] = []
    for x in range(0, ink.shape[1], 2):
        edges = np.diff(np.concatenate([[0], ink[:, x], [0]]))
        runs.extend(
            int(r) for r in np.where(edges == -1)[0] - np.where(edges == 1)[0] if 1 <= r < 30
        )
    if not runs:
        return 3.0
    # Основная линия — верхняя часть распределения толщин (тонкие вдвое тоньше).
    return float(np.percentile(runs, 70))


def prepare(
    gray: Any, box: tuple[int, int, int, int], margin: float = 0.03
) -> tuple[Any, float, tuple[int, int]]:
    """Вырез области с полем, увеличенный до рабочей толщины линии.

    Возвращает (изображение, коэффициент увеличения, начало выреза)."""
    import cv2
    import numpy as np

    g = np.asarray(gray)
    x0, y0, x1, y1 = box
    pad = int(margin * max(x1 - x0, y1 - y0))
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(g.shape[1], x1 + pad), min(g.shape[0], y1 + pad)
    crop = g[y0:y1, x0:x1]
    line = _line_px(crop)
    factor = max(1.0, min(4.0, _WORK_LINE_PX / max(line, 1.0)))
    if factor > 1.05:
        crop = cv2.resize(crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
    return crop, factor, (x0, y0)


def _symmetric_orientation(gray: Any, line: float) -> tuple[bool, float]:
    """(вертикальная ли ось, сила симметрии) — по горизонтальным штрихам.

    Пара линий, зеркальных относительно оси, на скане и после увеличения
    расходится на 1–2 px: попиксельное совпадение давало ей оценку не выше
    случайной, и колесо p009 (ось горизонтальна) поворачивалось набок.
    Совпадение — с допуском в толщину линии поперёк.
    """
    import cv2
    import numpy as np

    from app.ai.cad_views.section_material import ink_mask

    def strength(image: Any) -> float:
        ink = ink_mask(image, line)
        horizontal = cv2.morphologyEx(
            ink, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
        )[::2, ::2]
        kernel = max(3, int(line)) | 1
        wide = cv2.dilate(horizontal, np.ones((kernel, 1), np.uint8))
        height = horizontal.shape[0]
        best = 0.0
        for y in range(height // 5, 4 * height // 5):
            half = min(y, height - y)
            above, below = horizontal[y - half : y], horizontal[y : y + half][::-1]
            wide_above, wide_below = wide[y - half : y], wide[y : y + half][::-1]
            matched = float((above & wide_below).sum()) + float((below & wide_above).sum())
            best = max(best, matched / (float(above.sum() + below.sum()) + 1.0))
        return best

    straight = strength(gray)
    turned = strength(np.ascontiguousarray(np.rot90(gray)))
    return (turned > 1.05 * straight), max(straight, turned)


def lifted_rim(gray: Any, profile: Any, line: float) -> Any:
    """Площадка материала, поднятая до основной линии над незаштрихованной
    полосой (зубья в разрезе не штрихуют, ГОСТ 2.402: штриховка кончается у
    впадин, вершины — основная линия выше). None — такой линии нет."""
    import cv2
    import numpy as np

    from app.ai.cad_views.extrude_body import main_line_mask
    from app.ai.cad_views.revolve_profile import HalfProfile, plateaus

    # Только основные линии: тонкие (выносная знака шероховатости, делительная
    # окружность штрихпунктиром) лежат в той же полосе над штриховкой.
    _ink, thick, _line = main_line_mask(gray)
    horizontal = cv2.morphologyEx(
        thick, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
    )
    axis = int(round(profile.axis_y))
    lifted = list(profile.outer)
    changed = False
    for xa, xb, radius in plateaus(profile.outer, 3 * line):
        found = []
        columns = range(int(xa) + int(line), int(xb) - int(line))
        for x in columns:
            top = int(axis - radius - 1.5 * line)
            limit = int(axis - 1.3 * radius)
            hit = None
            for y in range(top, max(limit, 0), -1):
                if horizontal[y, x]:
                    hit = axis - y
                    break
            if hit is not None:
                found.append(hit)
        if len(columns) < 3 or len(found) < 0.8 * len(columns):
            continue
        level = float(np.median(found))
        if np.std(found) > line or level - radius < 2 * line:
            continue
        lifted = [(x, level if xa <= x <= xb and abs(r - radius) < line else r) for x, r in lifted]
        changed = True
    if not changed:
        return None
    return HalfProfile(
        axis_y=profile.axis_y,
        line_px=profile.line_px,
        x0=profile.x0,
        x1=profile.x1,
        outer=lifted,
        inner=profile.inner,
    )


def tolerant_axis(mask: Any, line: float) -> int:
    """Ось симметрии горизонталей с допуском в толщину линии.

    Попиксельное совпадение зеркальных линий ломается о разницу в толщину:
    у втулки p015 (половина вида и половина разреза) верная ось проигрывала
    ложной, относительно которой симметричны образующая и линия расточки."""
    import cv2
    import numpy as np

    small = mask[::2, ::2]
    kernel = max(3, int(line)) | 1
    wide = cv2.dilate(small, np.ones((kernel, 1), np.uint8))
    height = small.shape[0]
    best = (-1.0, height // 2)
    for y in range(height // 5, 4 * height // 5):
        half = min(y, height - y)
        above, below = small[y - half : y], small[y : y + half][::-1]
        wide_above, wide_below = wide[y - half : y], wide[y : y + half][::-1]
        score = float((above & wide_below).sum()) + float((below & wide_above).sum())
        if score > best[0]:
            best = (score, y)
    # Уточнение: точное совпадение в окне ± толщина линии вокруг грубой оси.
    coarse = 2 * best[1]
    full_height = mask.shape[0]
    refined = (-1.0, coarse)
    for y in range(max(1, coarse - int(line) - 1), min(full_height - 1, coarse + int(line) + 2)):
        half = min(y, full_height - y)
        above = mask[y - half : y].astype(np.float32)
        below = mask[y : y + half][::-1].astype(np.float32)
        score = float((above * below).sum()) / (float(above.sum() + below.sum()) + 1.0)
        if score > refined[0]:
            refined = (score, y)
    return refined[1]


def _drop_section_traces(half: list[Any], gray: Any, axis: int, line: float) -> list[Any]:
    """Следы секущих плоскостей (ГОСТ 2.305) — не бурты.

    След — короткий толстый штрих над и под видом со стрелкой и буквой;
    выносные связывают его с контуром, и силуэт «вырастал» буртом Ø52 и
    Ø99 у вала Ø50 (многоосевой вал 0). У бурта две боковые кромки от
    соседней ступени до его образующей, у следа — один штрих."""

    from app.ai.cad_views.extrude_body import main_line_mask

    values = [h for h in half if h is not None]
    if len(values) < 10:
        return half
    _ink, thick, _line = main_line_mask(gray)
    height, width = thick.shape[:2]
    import cv2
    import numpy as np

    from app.ai.cad_views.section_material import ink_mask

    horizontal = cv2.morphologyEx(
        ink_mask(gray, line), cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
    )
    out = list(half)
    # Площадки — участки почти одного уровня (скачок больше полутора линий).
    plateaus: list[list[int]] = []
    for x, h in enumerate(out):
        if h is None:
            continue
        # С уровнем начала площадки, а не с соседним столбцом: скаты стрелок
        # размера дают плавный подъём, и выброс Ø28 над Ø14 сливался с
        # площадкой (многоосевой вал 2).
        if plateaus and x - plateaus[-1][1] <= 2 and abs(h - out[plateaus[-1][0]]) <= 1.5 * line:
            plateaus[-1][1] = x
        else:
            plateaus.append([x, x])

    # Сосед — ближайшая площадка длиннее линии: наклонный штрих буквы или
    # стрелки у следа даёт «лестницу» однопиксельных площадок, и соседом
    # следа становилась её верхняя ступенька (вал shaft-6: «Г» над Ø14 —
    # Ø64, сосед 166 px при уровне 181, «не выше 1,3 соседа»).
    def neighbour(index: int, step: int) -> int | None:
        edge = plateaus[index][0] if step < 0 else plateaus[index][1]
        start = index
        index += step
        while 0 <= index < len(plateaus):
            a, b = plateaus[index]
            if b - a + 1 >= line:
                return index
            # Лестница — не дальше трёх линий; дальше сосед прежний.
            if abs((b if step < 0 else a) - edge) > 3 * line:
                break
            index += step
        index = start + step
        return index if 0 <= index < len(plateaus) else None

    for index, (x, end) in enumerate(plateaus):
        if index == 0 or index == len(plateaus) - 1:
            continue
        before, after = neighbour(index, -1), neighbour(index, 1)
        if before is None or after is None:
            continue
        level = max(out[k] for k in range(x, end + 1))
        left = out[plateaus[before][1]]
        right = out[plateaus[after][0]]
        base = max(left, right)
        if (end - x) > 0.15 * len(values) or level <= 1.3 * base:
            continue
        # Боковые кромки: вертикали основной линии от base до level над осью.
        top, bottom = int(axis - level + line), int(axis - base - line)
        sides = 0
        if 0 <= top < bottom <= height:
            # Кромка бурта доходит до контура соседней ступени; штрих следа
            # секущей висит над ним с зазором (многоосевой вал 5: след «В»
            # у уступа и буква давали две «кромки» бурта Ø61).
            foot = max(top, bottom - int(1.5 * line))
            columns = [
                c
                for c in range(max(0, x - int(line)), min(width, end + int(line) + 1))
                if thick[top:bottom, c].mean() >= 0.7 and thick[foot:bottom, c].mean() >= 0.7
            ]
            groups: list[list[int]] = []
            for c in columns:
                if groups and c - groups[-1][-1] <= 2:
                    groups[-1].append(c)
                else:
                    groups.append([c])
            # Кромки бурта — на его концах, и разнесены почти на всю ширину:
            # стрелка размера без тонкой линии внутри распадается на две
            # половины посередине (многоосевой вал 2: выброс Ø28 над Ø14).
            centres = [(g[0] + g[-1]) / 2.0 for g in groups]
            spread = (max(centres) - min(centres)) if centres else 0.0
            sides = len(groups) if spread >= 0.6 * max(1, end - x) else min(len(groups), 1)
        if sides < 2:
            # Со следом уходит и лестница до соседних площадок. Столбец
            # получает свою пару линий ниже следа, а не уровень соседа: след
            # «В» над концом Ø16 заливался уровнем Ø22, и уступ уезжал на
            # 24 px влево (shaft-6).
            for k in range(plateaus[before][1] + 1, plateaus[after][0]):
                if out[k] is not None and out[k] > base:
                    own = _pair_below(horizontal, axis, line, k, level - line)
                    # Пара чуть ниже соседа — тот же контур, прерванный
                    # штрихом следа (shaft-3: Ø13,2 при Ø14 под следом «Б»);
                    # заметно ниже — своя ступень (shaft-6: конец Ø16).
                    out[k] = own if own is not None and own <= base - 1.5 * line else base
    return out


def _pair_below(horizontal: Any, axis: int, line: float, x: int, limit: float) -> float | None:
    """Наибольшая пара горизонталей, симметричных оси, ниже ``limit`` (как
    в `_silhouette`, для одного столбца)."""
    import numpy as np

    if not 0 <= x < horizontal.shape[1]:
        return None
    edges = np.diff(np.concatenate([[0], horizontal[:, x], [0]]))
    centres = [
        (a + b - 1) / 2.0
        for a, b in zip(np.where(edges == 1)[0], np.where(edges == -1)[0])
        if b - a >= 0.6 * line
    ]
    up = [axis - c for c in centres if c < axis - line and axis - c < limit]
    down = [c - axis for c in centres if c > axis + line and c - axis < limit]
    pairs = [r for r in up if any(abs(r - q) <= 1.5 * line for q in down)]
    return max(pairs) if pairs else None


def bore_from_lines(thick: Any, axis: int, line: float, outer: Any, material: Any = None) -> Any:
    """Полупрофиль «наружный контур + расточка по линиям» для разреза.

    Расточка в столбце — ближайшая к оси горизонталь основной линии выше и
    ниже оси (пара симметрична в пределах толщины линии), не дальше
    полутора линий от наружного контура. Расточка есть, если пара найдена
    хотя бы в половине столбцов; пропуски (поперечный канал, надпись) —
    по соседям.
    """
    import cv2
    import numpy as np

    from app.ai.cad_views.revolve_profile import HalfProfile

    horizontal = cv2.morphologyEx(
        thick, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
    )
    height = horizontal.shape[0]
    xs = [x for x, _r in outer.outer]
    rs = [r for _x, r in outer.outer]
    x0, x1 = int(outer.x0), int(outer.x1)
    # Полувид-полуразрез (ЕСКД): штриховка только по одну сторону оси —
    # расточку видно там одной стенкой, пары нет (втулка p008).
    one_side = 0
    if material is not None:
        rows = np.nonzero(material)[0]
        if rows.size:
            below = float((rows > axis).mean())
            one_side = 1 if below > 0.9 else -1 if below < 0.1 else 0
    found: list[float | None] = []
    for x in range(x0, x1 + 1):
        reach = float(np.interp(x, xs, rs)) - 1.5 * line
        radius: float | None = None
        if one_side and 0 <= x < horizontal.shape[1] and reach > line:
            start = int(axis + one_side * 0.6 * line)
            stop = int(axis + one_side * reach)
            hit = next(
                (y for y in range(start, stop, one_side) if 0 <= y < height and horizontal[y, x]),
                None,
            )
            if hit is not None:
                end = hit
                while 0 <= end + one_side < height and horizontal[end + one_side, x]:
                    end += one_side
                candidate = abs((hit + end) / 2.0 - axis)
                near = int(axis + one_side * (candidate + line))
                far = int(axis + one_side * min(reach, candidate + 2.5 * line))
                lo, hi = min(near, far), max(near, far)
                if material[max(0, lo) : max(0, hi), x].any():
                    radius = candidate
            found.append(radius)
            continue
        if 0 <= x < horizontal.shape[1] and reach > line:
            pair = []
            for sign in (-1, 1):
                start = int(axis + sign * 0.6 * line)
                stop = int(axis + sign * reach)
                rows = range(start, stop, sign)
                hit = next((y for y in rows if 0 <= y < height and horizontal[y, x]), None)
                if hit is None:
                    break
                end = hit
                while 0 <= end + sign < height and horizontal[end + sign, x]:
                    end += sign
                pair.append(abs((hit + end) / 2.0 - axis))
            if len(pair) == 2 and abs(pair[0] - pair[1]) <= line:
                radius = (pair[0] + pair[1]) / 2.0
                # Стенка расточки — край разреза: за ней материал. Контур
                # паза лицом тоже пара горизонталей у оси, но за ним пусто
                # (сплошной вал shaft-6 получал «расточку» по пазам).
                if material is not None:
                    # Материал — вплотную за линией (в 2,5 линии): за штрихом
                    # надписи на оси («Ø15», shaft-2) сначала пустая расточка.
                    top = int(axis - min(reach, radius + 2.5 * line))
                    bottom = int(axis - radius - line)
                    low = int(axis + radius + line)
                    high = int(axis + min(reach, radius + 2.5 * line))
                    if not (
                        material[max(0, top) : max(0, bottom), x].any()
                        or material[max(0, low) : max(0, high), x].any()
                    ):
                        radius = None
        found.append(radius)
    known = [value for value in found if value is not None]
    if len(known) < 0.5 * len(found) or not known:
        return None
    inner = _levelled(found, x0, line, min_run=4 * line)
    # Наружный контур — тоже по линиям: самая дальняя от оси длинная
    # горизонталь в пределах тела (с любой стороны: шпоночный паз снизу
    # не делает вал тоньше). Материал под надписью без штриховки «проседал»
    # (shaft-7: Ø23,9 вместо Ø28 под «Ø6»). Нет линии — прежний контур.
    limit = max(rs) + 2.0 * line
    outer_found: list[float | None] = []
    for i, x in enumerate(range(x0, x1 + 1)):
        floor = (found[i] or 0.0) + line
        best: float | None = None
        # Полуразрез: вторая половина — вид, её дальние горизонтали не контур;
        # наружный контур остаётся прежним.
        if 0 <= x < horizontal.shape[1] and not one_side:
            for sign in (-1, 1):
                far = int(axis + sign * limit)
                near = int(axis + sign * floor)
                hit = next(
                    (y for y in range(far, near, -sign) if 0 <= y < height and horizontal[y, x]),
                    None,
                )
                if hit is None:
                    continue
                end = hit
                while 0 <= end - sign < height and horizontal[end - sign, x]:
                    end -= sign
                radius = abs((hit + end) / 2.0 - axis)
                best = radius if best is None else max(best, radius)
        # Линия ниже прежнего контура — не наружная кромка: у торца с
        # фаской самой дальней горизонталью оказывалась стенка расточки
        # (shaft-5: площадка Ø13 на наружном контуре).
        previous = float(np.interp(x, xs, rs))
        outer_found.append(best if best is not None and best >= previous - 1.5 * line else previous)
    return HalfProfile(
        axis_y=float(axis),
        line_px=line,
        x0=x0,
        x1=x1,
        outer=_levelled(outer_found, x0, line, min_run=4 * line, ends=True),
        inner=inner,
    )


def _levelled(
    found: list[float | None],
    x0: int,
    line: float,
    *,
    min_run: float = 0.0,
    ends: bool = False,
) -> list[tuple[float, float]]:
    """Ломаная по столбцам: пропуски по соседям, медиана, уровни участков."""
    import numpy as np

    from app.ai.cad_views.revolve_profile import _median, _simplify

    index = [i for i, value in enumerate(found) if value is not None]
    filled = np.interp(range(len(found)), index, [found[i] for i in index])
    smooth = _median([float(v) for v in filled], int(2 * line) + 1)
    # Участок без скачка больше ¾ линии — один уровень (медиана): пара линий
    # дрожит на ±2 px, а площадка требует ровного радиуса.
    runs: list[list[int]] = []
    for i, value in enumerate(smooth):
        if runs and abs(value - smooth[i - 1]) <= 0.75 * line:
            runs[-1].append(i)
        else:
            runs.append([i])
    for run in runs:
        values = [smooth[i] for i in run]
        # Скос (фаска) — не площадка: разброс больше полутора линий остаётся
        # как есть (фаска 2×45° shaft-5 выравнивалась в ложную Ø32,7).
        if max(values) - min(values) > 1.5 * line:
            continue
        level = float(np.median(values))
        for i in run:
            smooth[i] = level
    # Участок короче ``min_run`` между соседями — не ступень: штрихи надписи
    # на оси («Ø15» полого вала shaft-2) давали расточку Ø3 на 4 мм.
    for index, run in enumerate(runs):
        if len(run) >= min_run or len(runs) < 2:
            continue
        edge = index == 0 or index == len(runs) - 1
        if edge and not ends:
            continue
        before = runs[index - 1] if index > 0 else []
        after = runs[index + 1] if index + 1 < len(runs) else []
        level = smooth[(before if len(before) >= len(after) else after)[0]]
        for i in run:
            smooth[i] = level
    return _simplify(
        [(float(x0 + i), float(v)) for i, v in enumerate(smooth)], max(1.0, 0.5 * line)
    )


def _has_hexagon(gray: Any, box: tuple[int, int, int, int]) -> bool:
    """На изображении есть правильный шестиугольник основных линий."""
    import cv2
    import numpy as np

    from app.ai.cad_views.extrude_body import main_line_mask

    x0, y0, x1, y1 = (int(v) for v in box)
    # Рамка области бывает впритык и срезает вершины (втулка p008).
    pad = int(0.06 * max(x1 - x0, y1 - y0))
    crop = np.asarray(gray)[max(0, y0 - pad) : y1 + pad, max(0, x0 - pad) : x1 + pad]
    if crop.size == 0:
        return False
    _ink, thick, line = main_line_mask(crop)
    contours, _h = cv2.findContours(thick.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    area = float(crop.shape[0] * crop.shape[1])
    for contour in contours:
        if cv2.contourArea(contour) < 0.05 * area:
            continue
        poly = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        if len(poly) != 6 or not cv2.isContourConvex(poly):
            continue
        points = poly.reshape(-1, 2).astype(float)
        sides = [float(np.linalg.norm(points[i] - points[(i + 1) % 6])) for i in range(6)]
        if max(sides) <= 1.2 * min(sides):
            return True
    return False


def _plain_steps(outer: list[dict[str, Any]]) -> list[tuple[float, float, float]]:
    """Площадки профиля: (Ø, начало, конец), мм."""
    steps = []
    for a, b in zip(outer, outer[1:], strict=False):
        if b["z"] - a["z"] > 0.3 and abs(a["r"] - b["r"]) <= 1e-6:
            steps.append((round(2.0 * float(a["r"]), 4), float(a["z"]), float(b["z"])))
    return steps


def _slanted_ends(
    half: list[Any], ink: Any, axis: int, line: float, x0: int, x1: int
) -> tuple[int, int]:
    """Силуэт, продлённый у торцов наклонной кромкой — конусом.

    Силуэт строится по горизонтальным штрихам, и конус под 45° стирается
    (фланец золотника p014 терял 7 из 10 мм). Продление — только прямой
    (у окружности соседнего вида кромка тоже наклонная, но не прямая),
    симметричной оси и продолжающей контур от торца."""
    import numpy as np

    def slanted(x: int) -> float | None:
        raw = np.diff(np.concatenate([[0], ink[:, x], [0]]))
        centres = [
            (a + b - 1) / 2.0
            for a, b in zip(np.where(raw == 1)[0], np.where(raw == -1)[0])
            if 0.9 * line <= b - a <= 2.5 * line
        ]
        up = [axis - c for c in centres if c < axis - line]
        down = [c - axis for c in centres if c > axis + line]
        pairs = [r for r in up if any(abs(r - q) <= 1.5 * line for q in down)]
        return max(pairs) if pairs else None

    def extend(start: int, step: int) -> int:
        points: list[tuple[int, float]] = []
        previous = half[start]
        x = start + step
        missed = 0
        # Угол у торца уступа — пятно, не штрих: несколько столбцов без пары.
        while 0 <= x < len(half) and previous is not None:
            r = slanted(x)
            if r is None or abs(r - previous) > 2.0 * line + missed * 5.0:
                missed += 1
                if missed > 2 * line:
                    break
                x += step
                continue
            missed = 0
            points.append((x, r))
            previous = r
            x += step
        if len(points) < 3 * line:
            return start
        xs = np.array([p[0] for p in points], float)
        rs = np.array([p[1] for p in points], float)
        slope, intercept = np.polyfit(xs, rs, 1)
        if not 0.2 <= abs(slope) <= 5.0:
            return start
        if np.abs(rs - (slope * xs + intercept)).max() > line:
            return start
        for px, pr in points:
            half[px] = pr
        return points[-1][0]

    return extend(x0, -1), extend(x1, +1)


def silhouette_profile(gray: Any, line: float, axis: int | None = None) -> Any:
    """Профиль тела вращения по неразрезанному виду: силуэт, без расточки."""
    import cv2
    import numpy as np

    from app.ai.cad_views.revolve_profile import (
        HalfProfile,
        _despike,
        _median,
        _simplify,
        drop_fins,
    )
    from app.ai.cad_views.section_material import ink_mask
    from app.ai.cad_views.view_features import _silhouette

    ink = ink_mask(gray, line)
    # Контур детали связан выносными в одну крупную фигуру; буквы
    # обозначений сечений («А», «Б») и подписи — отдельные мелкие фигуры, а
    # их горизонтальные штрихи симметричны оси и давали ложные бурты (p121:
    # Ø65 при наибольшем Ø50).
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    if count > 2:
        areas = stats[1:, cv2.CC_STAT_AREA]
        keep = np.zeros(count, bool)
        keep[1:] = areas >= 0.2 * areas.max()
        ink = (keep[labels] & (ink > 0)).astype(ink.dtype)
    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
    )
    if axis is None:
        axis = tolerant_axis(horizontal, line)
    half = drop_fins(
        _median(_despike(_silhouette(ink, axis, line), int(2 * line)), int(2 * line) + 1),
        int(2.5 * line),
    )
    half = _drop_section_traces(half, gray, axis, line)
    present = [x for x, h in enumerate(half) if h is not None]
    if not present:
        return None
    # Разрыв силуэта (паз, подпись поверх кромки) до четверти длины — та же деталь.
    gap = max(4 * line, 0.25 * (present[-1] - present[0]))
    runs: list[list[int]] = []
    for x in present:
        if runs and x - runs[-1][1] <= gap:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    x0, x1 = max(runs, key=lambda r: r[1] - r[0])
    x0, x1 = _slanted_ends(half, ink, axis, line, x0, x1)
    known = [x for x in range(x0, x1 + 1) if half[x] is not None]
    levels = [half[k] for k in known]
    points = [
        (float(x), float(np.interp(x, known, levels)) + line / 2.0) for x in range(x0, x1 + 1)
    ]
    return HalfProfile(
        axis_y=float(axis),
        line_px=line,
        x0=x0,
        x1=x1,
        outer=_simplify(points, max(1.0, 0.5 * line)),
    )


def _same_feature(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Тот же элемент: вид, ось, начало ближе 0,5 мм, размеры в 3 %."""
    import math

    if a.get("kind") != b.get("kind") or a.get("axis") != b.get("axis"):
        return False
    if math.dist(a["origin_mm"], b["origin_mm"]) > 0.5:
        return False
    for key in ("diameter_mm", "width_mm", "height_mm", "depth_mm"):
        va, vb = a.get(key), b.get(key)
        if (va is None) != (vb is None):
            return False
        if va is not None and abs(va - vb) > 0.03 * max(abs(va), abs(vb), 1e-6):
            return False
    return True


def section_features(
    gray: Any,
    main: Any,
    profile: Any,
    factor: float,
    origin: tuple[int, int],
    axial: float,
    outer: list[dict[str, float]],
    label_texts: list[str],
    region_labels: dict[int, list[str]] | None,
) -> list[dict[str, Any]]:
    """Элементы на вынесенных сечениях вала — проверенный путь спека (У6).

    Профиль и масштаб — в пикселях выреза; следы и сечения ищутся по листу,
    поэтому всё переводится в пиксели листа."""
    import math
    from types import SimpleNamespace

    import numpy as np

    from app.ai.cad_recognize.verifiers.reconcile import placed_additions
    from app.ai.cad_recognize.verifiers.section_outline import propose_placed
    from app.ai.cad_recognize.verifiers.section_traces import locate_section_traces
    from app.ai.cad_views.labels import parse_label

    # Ступени — площадки профиля (конусы и фаски не ступени).
    steps: list[dict[str, float]] = []
    for a, b in zip(outer, outer[1:]):
        length = float(b["z"]) - float(a["z"])
        if length <= 0 or abs(float(a["r"]) - float(b["r"])) > 1e-6:
            if steps and length > 0:
                steps[-1]["length_mm"] += length
            continue
        steps.append({"diameter_mm": round(2 * float(a["r"]), 3), "length_mm": length})
    if not steps:
        return []
    sheet_mm_per_px = axial * factor  # вырез увеличен в factor раз
    x0 = origin[0] + profile.x0 / factor
    x1 = origin[0] + profile.x1 / factor
    xs = [x for x, _r in profile.outer]
    rs = [r for _x, r in profile.outer]
    columns = np.arange(int(round(x0)), int(round(x1)) + 1)
    half = np.interp((columns - origin[0]) * factor, xs, rs) / factor
    sheet_profile = SimpleNamespace(
        x0=float(columns[0]),
        x1=float(columns[-1]),
        axis_y=origin[1] + profile.axis_y / factor,
        line_px=profile.line_px / factor,
        half_px=half,
    )
    frame = SimpleNamespace(mm_per_px=sheet_mm_per_px)
    try:
        traces = locate_section_traces(gray, frame, sheet_profile)
        if not traces:
            return []
        body = {"outer": steps, "placed_features": []}
        proposals = propose_placed(
            gray, tuple(main.box), sheet_mm_per_px, body, traces, sheet_profile
        )
    except Exception:  # noqa: BLE001 — элементы по сечениям необязательны
        return []
    if not proposals:
        return []
    texts = [*label_texts, *sum((region_labels or {}).values(), [])]
    spec = {"main_view": body, "dimensions": [{"value": t} for t in texts]}
    notes: list[str] = []
    found = []
    for addition in placed_additions(spec, {"placed_proposals": proposals}, notes):
        item = dict(addition["feature"])
        sheet = item.pop("sheet_station", None) or {}
        item["note"] = addition.get("reason") or "элемент по сечению"
        # Надписи, которые элемент объяснил, — его размеры, а не звенья
        # цепочки ступеней (m2: «40,55» лыски тянуло уступ 118 к 117,45).
        explains = [sheet.get("from_shoulder_mm")]
        if item.get("kind") == "pocket":
            radius = math.hypot(*item["origin_mm"][:2])
            explains += [item.get("width_mm"), 2.0 * radius - float(item.get("depth_mm") or 0.0)]
        item["_explains"] = [float(v) for v in explains if isinstance(v, (int, float))]
        found.append(item)
    # Не объяснённое надписями однозначно — по замеру (угол и Ø к надписям,
    # если близки): сечение мерит точно (29,8° при 30°, Ø6,02 при Ø6), а
    # строгое правило спека отдаёт такое человеку — здесь тело без элемента
    # хуже тела с замеренным.
    parsed = [parse_label(t) for t in texts]
    angles = sorted({lab.value for lab in parsed if lab.kind == "angle" and lab.value})
    holes = sorted({lab.value for lab in parsed if lab.kind == "diameter" and lab.value})
    starts = [0.0]
    for step in steps:
        starts.append(starts[-1] + step["length_mm"])
    for proposal in proposals:
        z = float(proposal["station_mm"])
        if any(abs(float(item["origin_mm"][2]) - z) <= 2.0 for item in found):
            continue
        index = proposal.get("step_index")
        if not isinstance(index, int) or not 0 <= index < len(steps):
            continue
        radius = steps[index]["diameter_mm"] / 2.0
        angle = float(proposal.get("angle_deg") or 0.0)
        near = [a for a in angles if abs(a - angle) <= 6.0]
        angle = near[0] if len(near) == 1 else round(angle / 5.0) * 5.0
        a = math.radians(angle)
        placement = {
            "origin_mm": [
                round(radius * math.cos(a), 4),
                round(radius * math.sin(a), 4),
                round(z, 3),
            ],
            "axis": [round(-math.cos(a), 6), round(-math.sin(a), 6), 0.0],
            "ref": [0.0, 0.0, 1.0],
        }
        if proposal["kind"] == "hole" and proposal.get("diameter_mm"):
            measured = float(proposal["diameter_mm"])
            close = [d for d in holes if abs(d - measured) <= max(0.5, 0.1 * d)]
            diameter = min(close, key=lambda d: abs(d - measured)) if close else round(measured, 1)
            item = {"kind": "hole", **placement, "diameter_mm": diameter, "through": True}
            if not proposal.get("through"):
                depth = next(
                    (
                        lab.depth
                        for lab in parsed
                        if lab.kind == "diameter" and lab.value == diameter and lab.depth
                    ),
                    None,
                )
                if depth:
                    item.update(through=False, depth_mm=depth)
            label = f"отверстие Ø{diameter:g}"
        elif proposal["kind"] == "pocket" and proposal.get("depth_mm"):
            length = proposal.get("length_mm") or min(steps[index]["length_mm"], 2.0 * radius)
            item = {
                "kind": "pocket",
                "profile": "rectangle",
                **placement,
                "width_mm": round(float(length), 3),
                "height_mm": round(2.0 * radius + 2.0, 3),
                "depth_mm": round(float(proposal["depth_mm"]), 3),
            }
            label = f"лыска глубиной {float(proposal['depth_mm']):g}"
            # Размер «поперёк» с листа (23,4 при замере 23,57) — тоже размер
            # лыски, не звено цепочки.
            item["_explains_near"] = [2.0 * radius - float(proposal["depth_mm"])]
        else:
            continue
        item["note"] = (
            f"по замеру сечения у {z:g} мм: {label} под {angle:g}° (надписи неоднозначны)"
        )
        found.append(item)
    return found


def _grown_pictures(gray: Any, pictures: list[Any]) -> list[Any]:
    """Рамки изображений, расширенные до основных линий, что выходят за край.

    Модель обводит изображение не целиком (шпиндель 793539cc_p013: «главный
    вид» на левую треть вала) — профиль обрывается на краю рамки, габарит
    ложится на часть детали. Контур детали — связные основные линии: рамка
    растёт до тех, что её пересекают (рамка листа и штамп — нет: они больше
    половины листа). Выбирают надписи.
    """
    from dataclasses import replace

    import cv2
    import numpy as np

    from app.ai.cad_views.extrude_body import main_line_mask

    g = np.asarray(gray)
    height, width = g.shape[:2]
    _ink, thick, line = main_line_mask(g)
    reach = max(3, int(round(1.5 * line)))
    joined = cv2.dilate(thick.astype(np.uint8), np.ones((reach, reach), np.uint8))
    _count, labels, stats, _c = cv2.connectedComponentsWithStats(joined, 8)
    grown = []
    for region in pictures:
        x0, y0, x1, y1 = (int(v) for v in region.box)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(width, x1), min(height, y1)
        if x1 <= x0 or y1 <= y0:
            continue
        box = [x0, y0, x1, y1]
        for index in np.unique(labels[y0:y1, x0:x1]):
            if index == 0:
                continue
            bx, by, bw, bh, _area = (int(v) for v in stats[index])
            if bw * bh > 0.5 * width * height or bw > 0.9 * width or bh > 0.9 * height:
                continue  # рамка листа, штамп
            box = [min(box[0], bx), min(box[1], by), max(box[2], bx + bw), max(box[3], by + bh)]
        if (box[2] - box[0]) * (box[3] - box[1]) >= 1.3 * (x1 - x0) * (y1 - y0):
            grown.append(replace(region, n=2000 + int(region.n), box=tuple(box)))
    return grown


def _axial_matches(
    gray: Any,
    crop: Any,
    profile: Any,
    factor: float,
    origin: tuple[int, int],
    vertical: bool,
    line: float,
    axial: float,
    labels: list[float],
    interpolate: bool = False,
) -> tuple[list[tuple[float, float, float]], float | None]:
    """Размерные линии вдоль оси с надписями: ([(a, b, мм)], масштаб) — a и b
    столбцы выреза, масштаб (мм/px выреза) — по самим линиям, если он
    объясняет больше надписей, чем заданный; иначе None.

    Ряды размеров лежат вне выреза изображения, поэтому ищутся на листе
    (повёрнутом, если ось вертикальна); пары выносных переводятся обратно в
    столбцы выреза."""
    import numpy as np

    from app.ai.cad_views.dimension_lines import axial_spans, match_spans, scale_from_spans

    g = np.asarray(gray)
    if vertical:
        # Вырез повёрнут rot90: столбец выреза — строка листа, строка выреза
        # i — столбец листа origin_x + (H − 1 − i) / factor.
        image = np.ascontiguousarray(np.rot90(g))
        along0 = origin[1]
        width = g.shape[1]
        height_crop = crop.shape[0]

        def across(i: float) -> float:
            return width - 1 - (origin[0] + (height_crop - 1 - i) / factor)

    else:
        image = g
        along0 = origin[0]

        def across(i: float) -> float:
            return origin[1] + i / factor

    xs = [along0 + x / factor for x, _r in profile.outer]
    rs = [r / factor for _x, r in profile.outer]
    if len(xs) < 2:
        return [], None

    def radius_at(x: float) -> float:
        if x < xs[0] or x > xs[-1]:
            return 0.0
        return float(np.interp(x, xs, rs))

    length_px = xs[-1] - xs[0]
    spans = axial_spans(
        image,
        xs[0],
        xs[-1],
        across(profile.axis_y),
        radius_at,
        line / factor,
        # Ряды размеров короткой толстой детали уходят дальше её длины
        # (золотник: габарит 30 — на 1,8 радиуса от оси).
        reach=max(rs) + max(0.3 * length_px, max(rs)),
        stations=xs,
    )
    pairs = [(s.a, s.b) for s in spans]
    sheet_scale = axial * factor
    found = None
    if not interpolate:
        # Масштаб по самим размерным линиям, если они объясняют больше
        # надписей, чем масштаб профиля (часть линий — от чужих выносных).
        by_lines, hits = scale_from_spans(pairs, labels)
        current = sum(
            1
            for a, b in pairs
            if any(abs((b - a) * sheet_scale - v) <= 0.015 * v for v in labels if v > 0)
        )
        # И длина профиля с ним — габарит (наибольшая надпись): плотный ряд
        # надписей даёт и случайные совпадения линий (p121, z4-r4).
        overall = max((v for v in labels if v > 0), default=None)
        length_px = (profile.x1 - profile.x0) / factor
        if (
            by_lines is not None
            and hits >= 3
            and hits > current
            and overall is not None
            and abs(length_px * by_lines - overall) <= 0.01 * overall
            # и с ним габарит сходится лучше, чем с прежним (shaft-4: 221,6
            # против 219,9 при 220 — прежний верен).
            and abs(length_px * by_lines - overall) < abs(length_px * sheet_scale - overall)
        ):
            sheet_scale = by_lines
            found = by_lines / factor
    # В масштабе размер обязан сойтись с масштабом листа (±10 %): по одному
    # порядку длин «56» ложилось на звено 18 мм.
    matched = match_spans(
        pairs,
        labels,
        scale=None if interpolate else sheet_scale,
        spread=2.0 if interpolate else 1.1,
        slack_px=2.0 * line / factor,
    )
    return [((a - along0) * factor, (b - along0) * factor, v) for a, b, v in matched], found


def _span_stations(
    profile: Any,
    matched: list[tuple[float, float, float]],
    line: float,
    axial: float,
    interpolate: bool = False,
) -> dict[float, float]:
    """{z замера, мм: номинал} — станции по размерным линиям листа."""
    from app.ai.cad_views.dimension_lines import stations_from_spans

    stations = sorted({x for x, _r in profile.outer} | {profile.x0, profile.x1})
    # В масштабе станции без размера остаются замером (их привяжет цепочка
    # надписей); не в масштабе — пропорционально между известными.
    solved = stations_from_spans(
        stations, matched, tolerance=max(1.5 * line, 2.0), interpolate=interpolate
    )
    return {
        round(round((x - profile.x0) * axial, 4), 6): round(nominal, 4)
        for x, nominal in solved.items()
    }


def _clip_to_overall(
    profile: Any, matched: list[tuple[float, float, float]], line: float, overall: float | None
) -> Any | None:
    """Профиль, обрезанный по выносным габарита, если он длиннее детали.

    Габарит — наибольший размер: один его конец на торце профиля, другой
    внутри — за ним не деталь, а захваченные линии (вал-шестерня part_01:
    выносной элемент «Б» у правого торца продлил вал на 9 мм, и все длины
    съехали на 12 %)."""
    from app.ai.cad_views.revolve_profile import HalfProfile

    # Только сам габарит — наибольшая надпись листа: наибольший из найденных
    # размеров бывает звеном («120» на z4-r4 при габарите 185).
    found = [m for m in matched if overall is not None and abs(m[2] - overall) <= 1e-6]
    if not found:
        return None
    a, b, _v = found[0]
    span = profile.x1 - profile.x0
    tol = max(2.0 * line, 0.005 * span)
    lo, hi = float(profile.x0), float(profile.x1)
    if abs(a - lo) <= tol and lo + 0.5 * span < b < hi - max(tol, 0.02 * span):
        hi = b
    elif abs(b - hi) <= tol and lo + max(tol, 0.02 * span) < a < hi - 0.5 * span:
        lo = a
    else:
        return None

    def clip(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        import numpy as np

        if not points:
            return points
        xs = [x for x, _r in points]
        rs = [r for _x, r in points]
        inside = [(x, r) for x, r in points if lo < x < hi]
        return [
            (lo, float(np.interp(lo, xs, rs))),
            *inside,
            (hi, float(np.interp(hi, xs, rs))),
        ]

    return HalfProfile(
        axis_y=profile.axis_y,
        line_px=profile.line_px,
        x0=int(round(lo)),
        x1=int(round(hi)),
        outer=clip(profile.outer),
        inner=clip(profile.inner) if profile.inner else [],
    )


def _figures_in(gray: Any, pictures: list[Any], limit: int = 4) -> list[Any]:
    """Отдельные фигуры внутри рамки, охватившей полстраницы.

    Модель обводит «главным видом» всю страницу чертежа с сечениями,
    выносными элементами и штампом (слайд c8d8313e_p005: вал занимает
    десятую часть рамки) — профиль строился не по валу. Фигура — связные
    основные линии; рамка листа и штамп (шире 90 % рамки) — не фигуры.
    Выбирают надписи."""
    from dataclasses import replace

    import cv2
    import numpy as np

    from app.ai.cad_views.extrude_body import main_line_mask

    g = np.asarray(gray)
    height, width = g.shape[:2]
    out = []
    for region in pictures:
        x0, y0, x1, y1 = (int(v) for v in region.box)
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(width, x1), min(height, y1)
        area = (x1 - x0) * (y1 - y0)
        if area < 0.2 * width * height:
            continue
        _ink, thick, line = main_line_mask(g[y0:y1, x0:x1])
        reach = max(3, int(round(2 * line)))
        joined = cv2.dilate(thick.astype(np.uint8), np.ones((reach, reach), np.uint8))
        count, _labels, stats, _c = cv2.connectedComponentsWithStats(joined, 8)
        figures = []
        for index in range(1, count):
            bx, by, bw, bh, _a = (int(v) for v in stats[index])
            if bw > 0.9 * (x1 - x0) or bh > 0.9 * (y1 - y0) or bw * bh < 0.01 * area:
                continue
            figures.append((bw * bh, (x0 + bx, y0 + by, x0 + bx + bw, y0 + by + bh)))
        for k, (_a, box) in enumerate(sorted(figures, reverse=True)[:limit]):
            out.append(replace(region, n=3000 + 10 * int(region.n) + k, box=box))
    return out


def _ordinal_diameters(
    outer: list[dict[str, float]],
    bore: list[dict[str, float]],
    shafts: list[float],
    holes: list[float],
    surplus: bool = True,
) -> tuple[list[dict[str, float]], list[dict[str, float]]] | None:
    """Ø площадок по порядку величины: наружные — наибольшие надписи,
    расточка — наименьшие (надписи отверстий «H» — только ей). None — число
    различных площадок не равно числу надписей."""

    length = max((float(p["z"]) for p in outer), default=0.0)

    def radii(points: list[dict[str, float]]) -> list[float]:
        # Площадка у торца короче 3 % длины — не ступень, а фаска или скругление,
        # нарисованные уступом (шпиндель: торец с фаской 1,5 — «Ø10,6» на
        # 0,9 мм); Ø ей — по соседней ступени.
        ends = {round(float(p["z"]), 4) for p in (points[:1] + points[-1:])}
        found = {
            round(float(a["r"]), 4)
            for a, b in zip(points, points[1:])
            if abs(float(a["r"]) - float(b["r"])) <= 1e-6
            and float(b["z"]) - float(a["z"]) > 1e-6
            and float(a["r"]) > 0
            and not (
                {round(float(a["z"]), 4), round(float(b["z"]), 4)} & ends
                and float(b["z"]) - float(a["z"]) < 0.03 * length
            )
        }
        return sorted(found)

    outer_r, bore_r = radii(outer), radii(bore)
    labels = sorted(set(shafts) | set(holes))
    if len(outer_r) + len(bore_r) > len(labels):
        # Площадок больше, чем надписей: узкая впадина между двумя более
        # высокими ступенями без своей надписи — прорезь или канавка, а не
        # ступень (золотник: прорезь 2,5 до расточки). Её Ø — по соседям.
        narrow = set()
        for i in range(1, len(outer) - 2):
            a, b = outer[i], outer[i + 1]
            r = round(float(a["r"]), 4)
            if (
                abs(float(a["r"]) - float(b["r"])) <= 1e-6
                and float(b["z"]) - float(a["z"]) < 0.05 * length
                and float(outer[i - 1]["r"]) > float(a["r"])
                and float(outer[i + 2]["r"]) > float(b["r"])
            ):
                narrow.add(r)
        kept = [r for r in outer_r if r not in narrow]
        if narrow and kept and len(kept) + len(bore_r) == len(labels):
            outer_r = kept
    if surplus and not bore_r and not holes and len(labels) > len(outer_r) >= 2:
        # Надписей больше, чем площадок: лишние — элементы, которых нет на
        # силуэте (резьбовое отверстие и выточка головки винта домкрата: Ø22,
        # M12 при ступенях Ø65 и Ø38). Наружным — наибольшие, если пропорции
        # замера с ними согласны (лист не в масштабе искажает их, но не
        # вдвое).
        top = labels[-len(outer_r) :]
        if all(
            0.5 <= (outer_r[i + 1] / outer_r[i]) / (top[i + 1] / top[i]) <= 2.0
            for i in range(len(top) - 1)
        ):
            labels = top
    if not outer_r or len(labels) != len(outer_r) + len(bore_r):
        return None
    inner_labels, outer_labels = labels[: len(bore_r)], labels[len(bore_r) :]
    if any(h not in inner_labels for h in holes):
        return None
    mapping_outer = dict(zip(outer_r, (d / 2.0 for d in outer_labels)))
    mapping_bore = dict(zip(bore_r, (d / 2.0 for d in inner_labels)))
    if bore_r and max(mapping_bore.values()) >= min(mapping_outer.values()):
        return None

    def apply(
        points: list[dict[str, float]], mapping: dict[float, float]
    ) -> list[dict[str, float]]:
        out = []
        for i, p in enumerate(points):
            r = round(float(p["r"]), 4)
            if r in mapping or r <= 0:
                out.append({**p, "r": mapping.get(r, float(p["r"]))})
                continue
            # Не ступень — в отношении ближайшей по оси площадки.
            near = min(
                (q for q in points if round(float(q["r"]), 4) in mapping),
                key=lambda q: abs(float(q["z"]) - float(p["z"])),
                default=None,
            )
            if near is None:
                out.append(dict(p))
                continue
            ratio = mapping[round(float(near["r"]), 4)] / float(near["r"])
            out.append({**p, "r": round(float(p["r"]) * ratio, 4)})
        return out

    return apply(outer, mapping_outer), apply(bore, mapping_bore)


def build_revolve(
    gray: Any,
    reading: Any,
    label_texts: list[str],
    *,
    part: str | None = None,
    region_labels: dict[int, list[str]] | None = None,
    unscaled: bool = False,
) -> ViewsResult:
    """Тело вращения по главному изображению и связанным видам листа.

    ``unscaled`` — лист не в масштабе (эскиз: втулка 793539cc_p015 — 9 мм
    по оси и Ø22 поперёк нарисованы с масштабами 0,016 и 0,032 мм/px, Ø13
    — как Ø10,7): осевой масштаб — по длинам независимо от радиального, Ø
    площадок — надписи по порядку величины, если их столько же, сколько
    площадок. Форма — с листа, числа — надписями, как читает инженер."""
    from collections import Counter

    import numpy as np

    from app.ai.cad_views.labels import parse_label
    from app.ai.cad_views.revolve_body import revolve_candidate, revolve_points
    from app.ai.cad_views.revolve_profile import (
        fit_axial_scale,
        fit_scale,
        plateaus,
        profile_from_material,
    )
    from app.ai.cad_views.section_material import ink_mask, section_material
    from app.ai.cad_views.view_features import side_view_features

    # Линейка бумаги — основная надпись ЕСКД (нет её — масштаб только по
    # надписям).
    try:
        from app.ai.cad_recognize.verifiers.sheet_scale import locate_title_block

        block = locate_title_block(gray)
    except Exception:  # noqa: BLE001 — лист без штампа или необычный
        block = None
    paper = block.paper_px_per_mm if block is not None else None
    if unscaled and paper is not None:
        # Лист с основной надписью ЕСКД начерчен в масштабе: «форма с листа,
        # числа надписями» на нём подбором объясняет почти любые надписи
        # (литой корпус-тройник p011 — «тело вращения» с 96 % надписей).
        return ViewsResult(False, "лист с основной надписью — в масштабе, эскизом не читается")
    pictures = [r for r in reading.regions if r.role in ("view", "section")]
    if part:
        pictures = [r for r in pictures if (r.part or "") == part] or pictures
    if not pictures:
        return ViewsResult(False, "на листе не найдено изображения детали")

    # Роль области модель путает: у вала p007 рамка «главного вида» стоит на
    # колонке размеров, а сам вал — в области «label». Крупные области без
    # роли изображения — тоже кандидаты (после названных); выбирают надписи.
    def area(region: Any) -> int:
        return (region.box[2] - region.box[0]) * (region.box[3] - region.box[1])

    largest = max(area(r) for r in pictures)
    spare = [
        r
        for r in reading.regions
        if r.role in ("label", "other") and area(r) >= 0.5 * largest and r not in pictures
    ]

    def label_sets(texts: list[str]) -> tuple[list[float], list[float], list[float], list[float]]:
        parsed = [parse_label(t) for t in texts]
        diameters = [
            lab.value for lab in parsed if lab.kind in ("diameter", "thread") and lab.value
        ]
        holes = [
            lab.value
            for lab in parsed
            if lab.kind == "diameter" and lab.surface == "hole" and lab.value
        ]
        shafts = [v for v in diameters if v not in holes]
        linear = [lab.value for lab in parsed if lab.kind == "linear" and lab.value]
        return diameters, holes, shafts, linear

    sheet_sets = label_sets(label_texts)

    def sets_for(region: Any) -> tuple[list[float], list[float], list[float], list[float]]:
        # Надписи своего изображения (вырез с полями под размеры) читаются
        # точнее: на плотном листе, ужатом целиком, числа пропадают. Диаметры
        # — объединением с листом (в вырез попадают не все Ø отверстий),
        # длины — свои, если они есть: габарит соседней детали деталировки
        # не должен становиться длиной этой.
        own = (region_labels or {}).get(region.n) or []
        if not own:
            return sheet_sets
        own_sets = label_sets(own)
        both = label_sets(own + list(label_texts))
        linear = own_sets[3] if len(set(own_sets[3])) >= 2 else both[3]
        return both[0], both[1], both[2], linear

    # Изображение для профиля: названное моделью главным — первым, затем
    # разрезы и виды по площади; берётся то, чей профиль надписи Ø объясняют
    # лучше всего. Живой /cad: модель назвала главным вид с торца, и тело
    # вышло по нему; порог — две объяснённые площадки (одна «Ø0,05» знака
    # биения давала деталь размером 0,05 мм).
    ordered = sorted(
        pictures,
        key=lambda r: (
            r.n != reading.main,
            r.role != "section",
            -(r.box[2] - r.box[0]) * (r.box[3] - r.box[1]),
        ),
    )
    notes: list[str] = []
    best = None
    tried: list[str] = []
    candidates = ordered[:6] + sorted(spare, key=lambda r: -area(r))[:2]
    candidates += _figures_in(gray, ordered[:2])
    if unscaled:
        # Не в масштабе масштаб не отсекает чужое — изображение берётся
        # целиком: рамка, обрезавшая деталь, давала её часть.
        candidates += _grown_pictures(gray, ordered[:6])
    for region in candidates:
        crop, factor, origin = prepare(gray, region.box)
        line = _line_px(crop)
        vertical, symmetry = _symmetric_orientation(crop, line)
        if symmetry < 0.4:
            tried.append(f"{region.name or region.n}: нет оси симметрии")
            continue
        if vertical:
            crop = np.ascontiguousarray(np.rot90(crop))
        # Контур упирается в край выреза — рамка области обрезала деталь
        # (колесо part_06: вершины зубьев Ø46 выше рамки). Инженер смотрит
        # шире: тот же вид с запасом побольше. Всем подряд запас не
        # расширяется — в вырез тогда попадают соседние размеры (part_02).
        probe = silhouette_profile(crop, line)
        if probe is not None:
            reach = max((r for _x, r in probe.outer), default=0.0)
            if min(probe.axis_y, crop.shape[0] - probe.axis_y) - reach <= 2 * line:
                crop, factor, origin = prepare(gray, region.box, margin=0.12)
                line = _line_px(crop)
                if vertical:
                    crop = np.ascontiguousarray(np.rot90(crop))
                probe = silhouette_profile(crop, line)
        # Разрез узнаётся по штриховке, а не по роли: роль от прогона к
        # прогону плавает («Опора»: главный вид в разрезе назван видом, вид
        # с торца — разрезом). Пробуются оба профиля, берётся лучше
        # объяснённый надписями.
        variants = []
        material_profiles = []
        # Ось — по всему изображению (силуэт основных линий): у половины
        # разреза штриховка с одной стороны, и ось по ней находится неверно.
        probe_axis = int(round(probe.axis_y)) if probe is not None else None
        material, axis = section_material(crop, line, axis=probe_axis)
        if material.sum() > 0:
            by_material = profile_from_material(material, axis, line, ink=ink_mask(crop, line))
            if by_material is not None:
                variants.append((by_material, True))
                material_profiles.append(by_material)
        if variants:
            rim = lifted_rim(crop, variants[0][0], line)
            if rim is not None:
                variants.append((rim, True))
        if material.sum() > 0:
            # Тот же разрез по ячейкам основных линий: размерные линии Ø и
            # поперечные каналы не режут стенку (`material_by_outline`).
            from app.ai.cad_views.section_material import material_by_outline

            outlined = material_by_outline(crop, line)
            if outlined.sum() > 0:
                by_cells = profile_from_material(outlined, axis, line, ink=ink_mask(crop, line))
                if by_cells is not None:
                    variants.append((by_cells, True))
                    material_profiles.append(by_cells)
        # Силуэт только по основным линиям: тонкие выноски, прошедшие через
        # контур, «достраивали» ступень конусом (многоосевой вал 5: выноска
        # «Ø6 120°» дала Ø60 и скос до Ø28). Вариант сверяется с надписями
        # наравне с остальными.
        import cv2

        from app.ai.cad_views.extrude_body import main_line_mask

        ink_all, thick_only, _ = main_line_mask(crop)
        thin_only = ink_all & (1 - cv2.dilate(thick_only, np.ones((3, 3), np.uint8)))
        if thin_only.any():
            main_only = crop.copy()
            main_only[thin_only > 0] = 255
            by_main_lines = silhouette_profile(main_only, line)
            if by_main_lines is not None:
                variants.append((by_main_lines, False))
            # Контур детали — одна связная фигура основных линий; следы
            # секущих (штрих со стрелкой над и под видом), буквы и стрелки
            # размеров — отдельные: силуэт по ним «вырастал» буртами (вал
            # с четырьмя сечениями).
            count, labels_cc, stats, _ = cv2.connectedComponentsWithStats(thick_only, 8)
            axis_row = probe_axis if probe_axis is not None else None
            if count > 2 and axis_row is not None:
                # Контур может рваться (паз, надпись поверх кромки) — берутся
                # все фигуры, пересекающие ось: торцы и уступы её пересекают,
                # следы, буквы и стрелки лежат над и под видом.
                straddle = [
                    index
                    for index in range(1, count)
                    if stats[index][cv2.CC_STAT_TOP] < axis_row - line
                    and stats[index][cv2.CC_STAT_TOP] + stats[index][cv2.CC_STAT_HEIGHT]
                    > axis_row + line
                ]
                outline_only = np.full_like(crop, 255)
                keep = cv2.dilate(
                    np.isin(labels_cc, straddle).astype(np.uint8), np.ones((3, 3), np.uint8)
                )
                outline_only[keep > 0] = crop[keep > 0]
                by_outline = silhouette_profile(outline_only, line)
                if by_outline is not None:
                    variants.append((by_outline, False))
        by_silhouette = silhouette_profile(crop, line)
        if material.sum() > 0 and probe_axis is not None:
            # Расточка по самим линиям: в разрезе полого вала её стенка —
            # длинная горизонталь основной линии, ближайшая к оси. Надпись
            # поперёк стенки (штриховка под ней прервана) и стрелки Ø режут
            # ячейки материала, линию — нет (shaft-7: Ø6 по ячейкам пропадала).
            # Наружный контур — от материала разреза (силуэт разреза полого
            # вала ловит саму расточку) и от силуэтов.
            outers = list(material_profiles) + [
                profile
                for profile, is_hatched in variants
                if not is_hatched and profile is not None
            ]
            if by_silhouette is not None:
                outers.append(by_silhouette)
            for outer_profile in outers[:4]:
                lined = bore_from_lines(thick_only, probe_axis, line, outer_profile, material)
                if lined is not None:
                    variants.append((lined, True))
        if by_silhouette is not None:
            variants.append((by_silhouette, False))
            # Зубья в осевом разрезе не штрихуют (ГОСТ 2.402): штриховка
            # кончается у впадин, наружный контур — основные линии силуэта,
            # расточка — по материалу (колесо p009: Ø78 по зубьям).
            for hatched in material_profiles:
                if not (hatched.inner and abs(hatched.axis_y - by_silhouette.axis_y) <= 2 * line):
                    continue
                from app.ai.cad_views.revolve_profile import HalfProfile

                # Границы детали — где есть и контур, и материал: за торец
                # уходит то материал (скосы стрелок Ø у торца вала-шестерни
                # p018), то силуэт (выносные у колеса part_06).
                x0 = max(by_silhouette.x0, hatched.x0)
                x1 = min(by_silhouette.x1, hatched.x1)
                if x1 - x0 > 4 * line:

                    def clip(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
                        kept = [(x, r) for x, r in points if x0 <= x <= x1]
                        if not kept:
                            return kept
                        xs = [x for x, _r in points]
                        rs = [r for _x, r in points]
                        head = (float(x0), float(np.interp(x0, xs, rs)))
                        tail = (float(x1), float(np.interp(x1, xs, rs)))
                        return [head, *[p for p in kept if x0 < p[0] < x1], tail]

                    inner = clip(hatched.inner)
                    outer = clip(by_silhouette.outer)
                    if inner and outer:
                        variants.append(
                            (
                                HalfProfile(
                                    axis_y=by_silhouette.axis_y,
                                    line_px=line,
                                    x0=x0,
                                    x1=x1,
                                    outer=outer,
                                    inner=inner,
                                ),
                                True,
                            )
                        )
        if not variants:
            tried.append(f"{region.name or region.n}: профиль не найден")
            continue
        diameters, holes, shafts, linear = sets_for(region)
        for profile, hatched in variants:
            radial, hits = fit_scale(profile, shafts, holes)
            # Габарит — самая надёжная надпись: масштаб, при котором длина
            # профиля с ним не сходится, объясняет Ø случайно («Опора»: 5 из
            # 11 Ø при длине 21 вместо 29). Габарит засчитывается как ещё одна
            # объяснённая надпись.
            overall = max(linear) / max(1, profile.x1 - profile.x0) if linear else None
            if overall is not None:
                along, along_hits = fit_scale(profile, shafts, holes, near=overall)
                if along is not None and along_hits + 1 >= hits and along_hits >= 1:
                    radial, hits = along, along_hits + 1
            if paper is not None and radial is not None:
                # Масштаб по штампу: основная надпись 185 × 55 мм — линейка
                # бумаги, масштаб изображения — из ряда ГОСТ 2.302. Масштаб
                # вне ряда — случайное совпадение надписей Ø; из масштабов
                # ряда берётся лучший по надписям, если объясняет хоть две.
                from app.ai.cad_recognize.verifiers.sheet_scale import _GOST_SCALES

                allowed = [
                    1.0 / (paper * factor * model / sheet_paper)
                    for model, sheet_paper in _GOST_SCALES
                ]
                if not any(abs(radial / g - 1.0) <= 0.04 for g in allowed):
                    options = []
                    for g in allowed:
                        found, found_hits = fit_scale(profile, shafts, holes, near=g, spread=0.04)
                        if found is not None and found_hits >= 2:
                            options.append((found_hits, found))
                    if options:
                        hits, radial = max(options)
            if unscaled and (radial is None or hits < 2):
                # Не в масштабе Ø площадок не объясняет ни один общий масштаб
                # (шпиндель: Ø13 нарисован как 11,6 при Ø18 как 18,7) — их
                # назначат надписи по порядку; масштаб — по габариту.
                radial, _along = fit_axial_scale(profile, linear)
                hits = 0
            source = "разрез" if hatched else "силуэт"
            tried.append(f"{region.name or region.n} ({source}): объяснено надписей {hits}")
            if radial is None or (hits < 2 and not unscaled):
                continue
            # По ЕСКД каждый диаметр образмерен: площадка намного больше
            # наибольшей надписи Ø — чужие линии в профиле (размерные,
            # выносные, соседний вид), а не деталь (p007: Ø192 при Ø56).
            widest = 2 * max((r for _x, r in profile.outer), default=0.0) * radial
            if diameters and widest > 1.15 * max(diameters):
                tried[-1] += f", но профиль шире наибольшего Ø ({widest:.1f} > {max(diameters):g})"
                continue
            # Вдоль оси то же: длиннее габарита деталь быть не может (p018:
            # профиль 220 при габарите 146 — захвачены линии за торцом).
            # Граница — наибольший размер всего листа: в вырез вида габарит
            # может не попасть («Опора», вид сверху: 12,5 при габарите 29).
            longest = (profile.x1 - profile.x0) * radial
            bound = max(linear + sheet_sets[3], default=0.0)
            # Вдвое длиннее наибольшей надписи — не захват линий за торцом, а
            # непрочитанный габарит (втулка p015: 18 при наибольшей «9»).
            if bound and 1.15 * bound < longest <= 1.6 * bound:
                tried[-1] += f", но профиль длиннее габарита ({longest:.1f} > {bound:g})"
                continue

            # Равное число объяснённых площадок — выигрывает профиль, у
            # которого они составляют большую долю: случайный масштаб
            # объясняет две площадки из многих (колесо p009 — 2 из 4 при
            # диаметре 92 вместо 78), верный — почти все.
            # Площадки — различными значениями: у разреза ступень дробится
            # размерными линиями и стрелками (Ø50 полого вала shaft-5 —
            # четыре отрезка 49,9…50,3), и доля объяснённых проигрывала
            # силуэту, у которого расточки нет вовсе.
            def distinct(radii: list[float]) -> int:
                kept: list[float] = []
                for radius in sorted(radii):
                    if not kept or radius > kept[-1] + max(profile.line_px, 0.03 * radius):
                        kept.append(radius)
                return len(kept)

            count = distinct(
                [p[2] for p in plateaus(profile.outer, 3 * profile.line_px)]
            ) + distinct([p[2] for p in plateaus(profile.inner, 3 * profile.line_px) if p[2] > 0])
            # Наибольшая надпись Ø — габарит по диаметру: при равном счёте
            # выигрывает профиль, чья наибольшая площадка её объясняет
            # (колесо part_06: вершины Ø46, а не впадины, совпавшие с Ø38).
            overall_d = max(shafts or diameters, default=0.0)
            fits_overall = bool(overall_d) and abs(widest - overall_d) <= 0.03 * overall_d
            # При прочих равных — профиль проще (меньше вершин): чертёж
            # вала — площадки, выбросы у каналов и надписей — не деталь
            # (shaft-2: ячейки материала давали расточку со ступенями 19…24).
            # Только между вариантами разреза: у силуэта меньше вершин бывает
            # и у варианта, потерявшего ступень (вал p121 — отказ проверки).
            vertices = len(profile.outer) + len(profile.inner or []) if hatched else 0
            key = (hits, fits_overall, round(hits / max(1, count), 2), hatched, -vertices)
            if unscaled:
                # Не в масштабе Ø назначаются по порядку — нужен вариант, где
                # площадок ровно столько, сколько надписей Ø (разрез с
                # расточкой, а не силуэт без неё).
                # Целиком деталь показывает то изображение, на изломах
                # которого лежит больше длин листа (часть вала объясняет
                # лишь свои: шпиндель — 10, 2, 22 из 127).
                _scale, along_hits = fit_axial_scale(profile, sheet_sets[3])
                # Площадкам хватает надписей — тем же правилом, что назначит
                # Ø (`_ordinal_diameters`: фаски у торцов и узкие прорези не
                # ступени).
                rough_outer, rough_bore = revolve_points(profile, radial, radial)
                # Точное совпадение числа площадок с надписями важнее
                # назначения с лишними надписями: вариант без расточки
                # «объяснял» Ø13 втулки лишним (p015).
                exact = (
                    _ordinal_diameters(
                        rough_outer, rough_bore, shafts or diameters, holes, surplus=False
                    )
                    is not None
                )
                fits = exact or (
                    _ordinal_diameters(rough_outer, rough_bore, shafts or diameters, holes)
                    is not None
                )
                key = (fits, exact, along_hits, *key)
            if best is None or key > best[0]:
                best = (key, region, crop, factor, origin, line, vertical, profile, radial, hits)
                chosen_sets = (diameters, holes, shafts, linear)
    if best is None:
        return ViewsResult(
            False,
            "ни одно изображение детали не объяснено надписями Ø (нужно от двух разных, габарит считается): "
            + "; ".join(tried),
            notes=notes,
        )
    _key, main, crop, factor, origin, line, vertical, profile, radial, hits = best
    diameters, holes, shafts, linear = chosen_sets
    notes.append("выбор изображения: " + "; ".join(tried))
    axial, _ = fit_axial_scale(profile, linear, near=None if unscaled else radial)
    axial = axial or radial
    # Размерные линии вдоль оси: надписи листа и изображения — объединением
    # с кратностью (одна надпись в обоих списках — один размер).
    try:
        matched, by_lines = _axial_matches(
            gray,
            crop,
            profile,
            factor,
            origin,
            vertical,
            line,
            axial,
            list((Counter(sheet_sets[3]) | Counter(linear)).elements()),
            interpolate=unscaled,
        )
    except Exception:  # noqa: BLE001 — размерные линии не обязательны
        matched, by_lines = [], None
    if by_lines is not None:
        notes.append(f"масштаб вдоль оси — по размерным линиям: {axial:.5f} → {by_lines:.5f} мм/px")
        axial = by_lines
        if abs(radial / axial - 1.0) > 0.05:
            # Лист в масштабе: поперёк — тот же масштаб; Ø — заново рядом с ним.
            again, again_hits = fit_scale(profile, shafts, holes, near=axial, spread=0.04)
            radial = again if again is not None and again_hits >= 2 else axial
    clipped = _clip_to_overall(profile, matched, line, max([*sheet_sets[3], *linear], default=None))
    if clipped is not None:
        notes.append(
            f"профиль обрезан по габариту: {profile.x1 - profile.x0} → {clipped.x1 - clipped.x0} px"
        )
        profile = clipped
        if by_lines is None:
            axial, _ = fit_axial_scale(profile, linear, near=None if unscaled else radial)
            axial = axial or radial
    outer, bore = revolve_points(profile, axial, radial)
    # C2: станции и Ø площадок — номиналы надписей (перечерчивание инженером).
    from app.ai.cad_views.nominals import nominal_revolve

    length = max((p["z"] for p in outer), default=0.0)
    if unscaled:
        # Ø — надписями по порядку ДО номиналов: привязка Ø к надписям по
        # замеру (радиальный масштаб здесь неверен) портила площадки, и
        # порядок уже не сходился (золотник).
        ordered = _ordinal_diameters(outer, bore, shafts or diameters, holes)
        if ordered is None:
            return ViewsResult(
                False,
                "лист не в масштабе, а надписей Ø не столько же, сколько площадок",
                notes=notes,
            )
        outer, bore = ordered
        # Эскиз токарной детали — площадки и короткие фаски. Наклонные
        # участки на заметной части длины — не тело вращения, а что-то,
        # подогнанное надписями (литой корпус p011: 34 % длины — «конусы»).
        total = max((float(p["z"]) for p in outer), default=0.0)
        sloped = sum(
            float(b["z"]) - float(a["z"])
            for a, b in zip(outer, outer[1:])
            if float(b["z"]) - float(a["z"]) > 1e-6 and abs(float(a["r"]) - float(b["r"])) > 1e-6
        )
        if total <= 0 or sloped > 0.15 * total:
            return ViewsResult(
                False,
                f"эскиз не похож на тело вращения: наклонные участки {sloped:.1f} из {total:.1f} мм",
                notes=notes,
            )
        notes.append("лист не в масштабе: форма с листа, Ø и длины — надписями")
    raw_outer, raw_bore = outer, bore

    station_map: dict[float, float] = {}

    def nominal(chain: list[float]) -> tuple[list[dict], list[dict], int]:
        station_map.clear()
        return nominal_revolve(
            raw_outer,
            raw_bore,
            chain,
            shafts or diameters,
            holes or diameters,
            # Эскиз не в масштабе: пропорции приблизительны (бурт 1,77 при 2).
            tolerance=max(1.2 * line * axial, (0.05 if unscaled else 0.006) * length),
            bore_share=0.07 if holes else 0.03,
            diameter_tolerance_mm=1.2 * line * radial,
            measured=measured,
            prefer_measured=unscaled,
            station_map=station_map,
            threads=[
                lab.value
                for lab in (
                    parse_label(t) for t in [*label_texts, *sum((region_labels or {}).values(), [])]
                )
                if lab.kind == "thread" and lab.value
            ],
        )

    try:
        measured = _span_stations(profile, matched, line, axial, interpolate=unscaled)
    except Exception:  # noqa: BLE001 — размерные линии не обязательны
        measured = {}
    if measured:
        notes.append(f"станции по размерным линиям листа: {len(measured)}")
    outer, bore, snapped = nominal(linear)
    if snapped:
        notes.append(f"номиналы надписей: исправлено {snapped} значений замера")
    candidate = revolve_candidate(outer, bore, part or main.part or "деталь")
    features: list[dict[str, Any]] = []
    # Виды той же детали в проекционной связи: перекрываются с главным по
    # столбцам (или строкам, если ось вертикальная).
    for region in pictures:
        # Элементы — по видам: сечение и разрез показывают деталь в другой
        # плоскости (z4-r4: сечения А-А и Б-Б дали ложные «лыски»).
        if region is main or region.role != "view":
            continue
        a0, a1 = (main.box[1], main.box[3]) if vertical else (main.box[0], main.box[2])
        b0, b1 = (region.box[1], region.box[3]) if vertical else (region.box[0], region.box[2])
        overlap = min(a1, b1) - max(a0, b0)
        if overlap < 0.6 * min(a1 - a0, b1 - b0):
            continue
        # Вырез вида — в тех же столбцах (строках), что главное изображение:
        # проекционная связь, тот же масштаб и то же увеличение.
        import cv2

        g = np.asarray(gray)
        if vertical:
            y0, y1 = origin[1], origin[1] + int(round(crop.shape[1] / factor))
            x0, x1 = region.box[0], region.box[2]
            pad = int(0.03 * (x1 - x0))
            piece = g[y0:y1, max(0, x0 - pad) : x1 + pad]
        else:
            x0, x1 = origin[0], origin[0] + int(round(crop.shape[1] / factor))
            y0, y1 = region.box[1], region.box[3]
            pad = int(0.03 * (y1 - y0))
            piece = g[max(0, y0 - pad) : y1 + pad, x0:x1]
        view_crop = (
            cv2.resize(piece, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
            if factor > 1.05
            else piece
        )
        if vertical:
            view_crop = np.ascontiguousarray(np.rot90(view_crop))
        found = side_view_features(view_crop, profile, axial, radial, diameters, linear)
        for item in found:
            item["view"] = region.name or f"рамка {region.n}"
            # Один и тот же элемент с двух перекрывающихся изображений
            # («вид сверху» дважды в разметке) — одна лыска, а не две: второй
            # карман «не касается материала», и ядро отвергало всё тело.
            if any(_same_feature(item, other) for other in features):
                continue
            features.append(item)
    # Шпоночные пазы лицом — и на самом главном виде (вал с пазами: главный
    # вид — снизу, лицом к пазам; shaft-6 собирался без трёх пазов). С него
    # берутся только пазы: лыски и отверстия главного вида — уже в профиле.
    if not vertical and not _key[3]:
        for item in side_view_features(crop, profile, axial, radial, diameters, linear):
            if item.get("keyway") and not any(_same_feature(item, other) for other in features):
                item["view"] = main.name or f"рамка {main.n}"
                features.append(item)
    # Лыски и радиальные отверстия — по вынесенным сечениям (У6): станция —
    # след секущей на главном виде, угол и размер — сечение, числа —
    # надписи листа. Многоосевые валы собирались без единого элемента.
    if not vertical:
        keyways = [f["keyway"] for f in features if f.get("keyway")]
        for item in section_features(
            gray, main, profile, factor, origin, axial, outer, label_texts, region_labels
        ):
            # Вырез паза на сечении — не радиальное отверстие (shaft-6: три
            # паза строились сквозными Ø4…6 поперёк вала).
            station = float(item["origin_mm"][2])
            if item.get("kind") == "hole" and any(
                a - 1.0 <= station <= b + 1.0 for a, b in keyways
            ):
                continue
            if not any(_same_feature(item, other) for other in features):
                features.append(item)
                notes.append(item.pop("note", "элемент по сечению"))
    # Длина паза — его размер, а не звено цепочки ступеней: «19» паза
    # притягивало уступ 139 к 157 − 19 = 138, и от неверной базы съезжала
    # вся цепочка (shaft-3). Номиналы — заново без размеров пазов.
    keyway_sizes = [float(f["width_mm"]) for f in features if f.get("keyway")]
    # И положение паза от уступа или торца слева — тоже его размер: «18,8»
    # первого паза тянуло уступ 165 к 183 − 18,8 = 164,2, и вся цепочка
    # съезжала на 0,8 мм (shaft-6).
    stations = sorted({round(float(p["z"]), 3) for p in raw_outer})
    keyway_positions = []
    for f in features:
        if f.get("keyway"):
            start = float(f["keyway"][0])
            left = [z for z in stations if z <= start + 1e-6]
            # Уступ на листе — иногда две близкие точки (край канавки и сам
            # уступ): положение — от обеих.
            keyway_positions += [start - z for z in left if left[-1] - z <= 1.0]
    explained = [v for f in features for v in f.pop("_explains", [])]
    explained_near = [v for f in features for v in f.pop("_explains_near", [])]
    if keyway_sizes or explained or explained_near:
        chain = [
            v
            for v in linear
            if all(abs(v - k) > 0.05 for k in [*keyway_sizes, *explained])
            and all(abs(v - k) > max(0.5, 0.03 * v) for k in [*keyway_positions, *explained_near])
        ]
        if chain != list(linear):
            outer, bore, _again = nominal(chain)
            candidate = revolve_candidate(outer, bore, part or main.part or "деталь")
    # Паз найден в замере, ступени — в номиналах: концы паза переводятся той
    # же привязкой станций (лист не в масштабе сдвигает паз вместе с уступами).
    from app.ai.cad_views.nominals import remap_station

    for item in features:
        if item.get("keyway") and station_map:
            z0, z1 = (remap_station(float(z), station_map) for z in item["keyway"])
            if z1 > z0:
                item["keyway"] = [round(z0, 3), round(z1, 3)]
                item["origin_mm"][2] = round((z0 + z1) / 2.0, 3)
    # Поверхность под поперечным отверстием — цилиндр по соседям, а не дуги
    # пересечения из разреза; фаска «c×45°» — у входа резьбы.
    from app.ai.cad_views.nominals import bridge_cross_holes, chamfer_threaded_end

    outer, bore, bridged = bridge_cross_holes(
        outer, bore, [f for f in features if f.get("kind") == "hole"]
    )
    if bridged:
        notes.append(f"зона поперечного отверстия — цилиндр по соседям: {bridged}")
    every = [parse_label(t) for t in [*label_texts, *sum((region_labels or {}).values(), [])]]
    chamfer_sizes = [
        lab.value
        for lab in every
        if lab.kind == "chamfer" and lab.value and abs((lab.angle or 45.0) - 45.0) < 1.0
    ]
    thread_sizes = [lab.value for lab in every if lab.kind == "thread" and lab.value]
    outer, chamfer_note = chamfer_threaded_end(outer, chamfer_sizes, thread_sizes)
    if chamfer_note:
        notes.append(chamfer_note)
    if bridged or chamfer_note:
        candidate = revolve_candidate(outer, bore, part or main.part or "деталь")
    # Шестигранник под ключ: на другом виде — сам шестиугольник, на листе —
    # пара «S» и «Ø по вершинам» (S / cos 30°), на профиле — ступень этого Ø;
    # грани через 60° (втулка шестигранная p008: S56, Ø65 строился
    # цилиндром). Одной пары надписей мало: 30 / cos 30° ≈ Ø35 совпадало на
    # валах без шестигранника. Вершины — сверху и снизу главного вида.
    hexagon_seen = any(_has_hexagon(gray, region.box) for region in pictures if region is not main)
    for step_d, z0, z1 in _plain_steps(outer) if hexagon_seen else []:
        flats = [
            v
            for v in linear
            if abs(v / math.cos(math.radians(30.0)) - step_d) <= 0.02 * step_d and v < step_d
        ]
        if len(flats) != 1:
            continue
        across = flats[0]
        radius = step_d / 2.0
        for k in range(6):
            a = math.radians(60.0 * k)
            features.append(
                {
                    "kind": "pocket",
                    "profile": "rectangle",
                    "origin_mm": [
                        round(radius * math.cos(a), 4),
                        round(radius * math.sin(a), 4),
                        round((z0 + z1) / 2.0, 3),
                    ],
                    "axis": [round(-math.cos(a), 6), round(-math.sin(a), 6), 0.0],
                    "ref": [0.0, 0.0, 1.0],
                    "width_mm": round(z1 - z0, 3),
                    "height_mm": round(step_d, 3),
                    "depth_mm": round(radius - across / 2.0, 3),
                    "source": f"шестигранник S{across:g}",
                }
            )
        notes.append(f"шестигранник S{across:g} на ступени Ø{step_d:g} ({z0:g}…{z1:g} мм)")
    for item in features:
        params: dict[str, Any] = {
            "placement": {"origin": item["origin_mm"], "axis": item["axis"], "ref": item["ref"]}
        }
        for key in ("diameter_mm", "through", "profile", "width_mm", "height_mm", "depth_mm"):
            if key in item:
                params[key] = item[key]
        candidate["candidate"]["features"].append(
            {"kind": item["kind"], "params": params, "confidence": 0.6}
        )
    return ViewsResult(
        True,
        candidate=candidate,
        profile={
            "outer": outer,
            "bore": bore,
            "main_view": main.name or main.n,
            "role": main.role,
            "source_box": list(main.box),
        },
        features=features,
        scales={
            # Мм на пиксель ЛИСТА: вырез увеличен в factor раз.
            "radial_mm_per_px": radial * factor,
            "axial_mm_per_px": axial * factor,
            "diameters_explained": hits,
        },
        notes=notes,
    )


async def read_labels(gray: Any, *, router: Any = None, confidential: bool = True) -> list[str]:
    """Надписи листа одним вопросом модели (без схемы спека)."""
    from PIL import Image

    from app.ai.cad_recognize.spec_fragments import _ask

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    image = Image.fromarray(gray)
    image.thumbnail((2400, 2400))
    answer = await _ask(
        LABELS_PROMPT,
        image,
        router=router,
        confidential=confidential,
        num_predict=3000,
        schema=LABELS_SCHEMA,
        timeout_seconds=150.0,
    )
    return [str(t) for t in (answer or {}).get("labels") or [] if str(t).strip()]


LABELS_VIEW_PROMPT = (
    "Это вырез технического чертежа: одно изображение детали с размерами вокруг. "
    "Выпиши ВСЕ размерные надписи на этом вырезе ровно так, как они написаны "
    "(с Ø, R, M, допусками, «гл.», «N отв.», фасками «1×45°», углами). Числа "
    "переписывай цифра в цифру; обрезанную краем надпись не выписывай; "
    "содержимое таблиц, штампа и технических требований не выписывай. Ответ — "
    'ОДНОЙ строкой JSON: {"labels": ["...", "..."]}'
)


async def read_region_labels(
    gray: Any,
    regions: list[Any],
    *,
    router: Any = None,
    confidential: bool = True,
    limit: int = 4,
) -> dict[int, list[str]]:
    """Надписи каждого изображения детали — по его вырезу с полями под размеры."""
    from PIL import Image

    from app.ai.cad_recognize.spec_fragments import _ask

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    height, width = gray.shape[:2]
    out: dict[int, list[str]] = {}
    ordered = sorted(regions, key=lambda r: -(r.box[2] - r.box[0]) * (r.box[3] - r.box[1]))
    for region in ordered[:limit]:
        x0, y0, x1, y1 = region.box
        pad = int(0.3 * max(x1 - x0, y1 - y0))
        crop = gray[
            max(0, y0 - pad) : min(height, y1 + pad), max(0, x0 - pad) : min(width, x1 + pad)
        ]
        image = Image.fromarray(crop)
        longest = max(image.size)
        if longest < 1400:
            factor = min(3.0, 1400 / max(1, longest))
            image = image.resize(
                (int(image.size[0] * factor), int(image.size[1] * factor)), Image.LANCZOS
            )
        image.thumbnail((2000, 2000))
        answer = await _ask(
            LABELS_VIEW_PROMPT,
            image,
            router=router,
            confidential=confidential,
            num_predict=2000,
            schema=LABELS_SCHEMA,
            timeout_seconds=120.0,
        )
        out[region.n] = [str(t) for t in (answer or {}).get("labels") or [] if str(t).strip()]
    return out


# Ниже этой доли надписей на теле основа не принимается: у заведомо неверных
# тел реальных листов (литой корпус как тело вращения, «вал» 6 × 14) на теле
# 22–38 % надписей, у верных — от 44 % (сплошной вал-шестерня) до 100 %.
_MIN_COVERAGE = 0.40
_COVERAGE_MIN_LABELS = 5


def choose_body(
    gray: Any,
    reading: Any,
    labels: list[str],
    region_labels: dict[int, list[str]],
    merged: list[str],
) -> ViewsResult:
    """Обе основы (вращение, выдавливание) — берётся та, на которой больше
    надписей листа (E1); если и на лучшей их мало — честный отказ."""
    from app.ai.cad_views.checks import label_coverage
    from app.ai.cad_views.extrude_body import build_extrude
    from app.ai.cad_views.prismatic import build_prismatic

    # Сборочный чертёж (общий вид) — не деталь: номера позиций 1…N на
    # полках. Модель называла такой лист деталью, и метод строил «деталь»
    # из корпуса сборки (реальный p076, «Серьга подвесная», позиции 1–6).
    positions = {int(t) for t in merged if re.fullmatch(r"\s*\d{1,2}\s*", str(t))}
    run = 0
    while run + 1 in positions:
        run += 1
    if run >= 5:
        return ViewsResult(
            False,
            f"похоже на сборочный чертёж: номера позиций 1…{run} — деталь по нему не строится",
        )
    revolved = build_revolve(gray, reading, labels, region_labels=region_labels)
    extruded = build_extrude(gray, reading, labels, region_labels=region_labels)
    prismatic = build_prismatic(gray, reading, labels, region_labels=region_labels)
    built = [r for r in (revolved, extruded, prismatic) if r.ok]
    if not built:
        return ViewsResult(
            False,
            f"тело вращения: {revolved.reason}; выдавливание: {extruded.reason}; "
            f"по трём видам: {prismatic.reason}",
            notes=revolved.notes + extruded.notes + prismatic.notes,
        )
    for candidate in built:
        candidate.coverage = label_coverage(candidate, merged)
    # Наибольшая линейная надпись — габарит детали: гипотеза, на теле которой
    # его нет, проигрывает при любой доле (корпус живьём: «пластина» вдвое
    # меньшего масштаба объясняла 86 % надписей половинками, но не «80»).
    from app.ai.cad_views.labels import parse_label

    lengths = [
        lab.value for lab in (parse_label(t) for t in merged) if lab.kind == "linear" and lab.value
    ]
    largest = max(lengths) if lengths else None

    def rank(result: ViewsResult) -> tuple[bool, bool, float]:
        explained = {
            lab.value
            for lab in (parse_label(t) for t in result.coverage.get("explained") or [])
            if lab.kind == "linear"
        }
        share = result.coverage.get("share") or 0.0
        # Только среди правдоподобных: габарит вне надписей бывает и у
        # верного тела (793539cc_p012: 0,78 у вала против 0,35 у «призмы»).
        return (share >= _MIN_COVERAGE, largest is None or largest in explained, share)

    best = max(built, key=rank)
    names = {
        id(revolved): "тело вращения",
        id(extruded): "выдавливание",
        id(prismatic): "по трём видам",
    }
    for other in (revolved, extruded, prismatic):
        if other is not best:
            share = other.coverage.get("share") if other.ok else None
            best.notes.append(
                names[id(other)]
                + (
                    f": надписей на теле {share:.0%}"
                    if share is not None
                    else f": {other.reason[:160]}"
                )
            )
    coverage = best.coverage
    total = len(coverage.get("explained") or []) + len(coverage.get("missing") or [])
    weak = (
        total >= _COVERAGE_MIN_LABELS
        and coverage.get("share") is not None
        and coverage["share"] < _MIN_COVERAGE
    )
    # Габарит вне тела — построена часть детали (рамка обрезала вал, масштаб
    # подобран по надписям этой части: шпиндель 22 мм из 127).
    partial = total >= _COVERAGE_MIN_LABELS and not rank(best)[1]
    if weak or partial:
        # Лист не в масштабе — форма с листа, числа надписями; принимается,
        # только если такое тело надписи объясняет (и габарит, если его не
        # объяснило тело в масштабе).
        sketch = build_revolve(gray, reading, labels, region_labels=region_labels, unscaled=True)
        if sketch.ok:
            sketch.coverage = label_coverage(sketch, merged)
            share = sketch.coverage.get("share") or 0.0
            if share >= _MIN_COVERAGE and (
                weak or (rank(sketch)[1] and share >= (coverage.get("share") or 0.0))
            ):
                sketch.notes.append(f"в масштабе листа надписей на теле {coverage['share']:.0%}")
                return sketch
    if (
        total >= _COVERAGE_MIN_LABELS
        and coverage.get("share") is not None
        and coverage["share"] < _MIN_COVERAGE
    ):
        return ViewsResult(
            False,
            f"тело не согласуется с надписями листа: на нём {len(coverage['explained'])} из "
            f"{total} (нет: {', '.join(coverage['missing'][:8])})",
            notes=best.notes,
            coverage=coverage,
        )
    return best


async def digitize_revolve(gray: Any, *, router: Any = None) -> tuple[ViewsResult, Any, list[str]]:
    """Лист → (результат, прочтение ролей, надписи)."""
    from app.ai.cad_views.sheet_reading import read_sheet

    reading = await read_sheet(gray, router=router)
    labels = await read_labels(gray, router=router)
    pictures = [r for r in reading.regions if r.role in ("view", "section")]
    region_labels = await read_region_labels(gray, pictures, router=router) if pictures else {}
    seen = set(labels)
    merged = list(labels)
    for texts in region_labels.values():
        for text in texts:
            if text not in seen:
                seen.add(text)
                merged.append(text)
    result = choose_body(gray, reading, labels, region_labels, merged)
    # Для повторной сверки без модели (scripts/eval_views_elements.py): метод
    # пользуется и надписями каждого изображения, без них повтор расходится
    # с живым прогоном.
    reading.sheet_labels = labels
    reading.region_labels = region_labels
    return result, reading, merged


# Лист → тело любой поддержанной основы (вращение, выдавливание).
digitize = digitize_revolve


__all__ = [
    "ViewsResult",
    "build_revolve",
    "digitize",
    "digitize_revolve",
    "read_labels",
    "read_region_labels",
]
