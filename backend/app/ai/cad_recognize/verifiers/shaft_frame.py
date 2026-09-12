"""Система координат главного вида тела вращения и его наружный профиль — по листу.

Осевая линия на перечерченном листе не обязательна, но профиль вала
симметричен: верхняя и нижняя кромка каждой ступени — пара горизонталей на
равном расстоянии от оси. Кандидаты в ось — середины, у которых таких пар
больше всего (вес пары — общая длина, умноженная на толщину более тонкой
линии: пара размерных линий тонкая и ось не перетянет). Кандидат принимается,
только если ось пересекают ОСНОВНЫЕ вертикали торцов на обоих концах профиля:
у полого вала два вида одного профиля стоят один под другим, и середина
промежутка между ними «симметрична» не хуже настоящей оси, но торцы через неё
не проходят (а вертикальные размерные линии Ø через ось проходят — они тонкие).
Толщина вертикали мерится у самой оси: торец сливается в одну линию с
выносной, которая от него начинается, и медиана по всей линии — тонкая.

Главная ось — первая по голосам из прошедших. Второй вид того же вала (торцы
на тех же столбцах) заменяет её, если его профиль больше по площади: в
разрезе полого вала паз опускает верхнюю кромку, симметричной пары там нет, и
профиль падал до расточки, а вид снизу показывает ту же ступень целиком.
(Площадь среди ВСЕХ прошедших — нельзя: нелепая пара далёких линий выигрывала.)

Профиль — для каждого столбца самая внешняя симметричная пара основных линий
(контур паза и расточка — внутри, размерные — тонкие), одним участком: вид с
торца стоит на той же оси, но отдельно. Масштаб — прочитанная общая длина на
расстояние между торцами.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Допуск симметрии пары, пиксели листа.
_SYMMETRY_PX = 2.0
# Основная линия — не тоньше этой доли эталонной толщины (тонкая — вдвое тоньше).
_MAIN_SHARE = 0.6
# Разрыв профиля, который ещё считается тем же видом, — доля всей протяжённости
# найденных столбцов. Внутри вида разрывы до 3,5 % (короткая ступень без пары,
# канавка: shaft-3 — 22 px, shaft-20 — 47 px), вид с торца отделён на 20–33 %.
# От доли текущего куска профиль рвался, и торец находился на середине вала.
_GAP_TOTAL_SHARE = 0.05
# Сколько лучших кандидатов оси проверять на торцы.
_AXIS_CANDIDATES = 6
# Второй вид того же вала: торцы на тех же столбцах в пределах этой доли длины.
_SAME_VIEW_SHARE = 0.02


@dataclass(frozen=True)
class ShaftProfile:
    """Наружный профиль: полувысота в пикселях для каждого столбца ``x0..x1``."""

    x0: int
    x1: int
    axis_y: float
    half_px: Any  # numpy: полувысота или nan, длина x1 - x0 + 1
    # Столбцы основных вертикалей внутри вида — грани уступов (центр линии).
    # (x, начало, конец по вертикали): у канавки у уступа тоже есть грань,
    # отличить уступ можно только по тому, какой скачок радиуса она покрывает.
    faces_px: tuple[tuple[float, float, float], ...] = ()
    # Эталонная толщина основной линии (масса поперёк, px) — мера разрешения.
    line_px: float = 0.0

    def half_at(self, x: float) -> float | None:
        import math

        index = int(round(x)) - self.x0
        if index < 0 or index >= len(self.half_px):
            return None
        value = float(self.half_px[index])
        return None if math.isnan(value) else value


@dataclass(frozen=True)
class _Sheet:
    gray: Any
    ink: Any
    main_weight: float


def locate_shaft_frame(sheet: Any, total_length_mm: float) -> tuple[ViewFrame, ShaftProfile] | None:
    """Главный вид вала → (система координат с началом на левом торце на оси, профиль)."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _lines, _stroke

    if not total_length_mm or total_length_mm <= 0:
        return None
    gray = np.asarray(sheet)
    ink = _ink(gray)
    min_length = max(6, int(round(0.006 * min(gray.shape))))
    lines = _segments(ink, min_length)
    if len(lines) < 2:
        return None
    # Нижний квартиль толщины: у короткой размерной линии залитые стрелки
    # занимают почти половину длины, и по медиане она выходила основной
    # (shaft-4: пара размерных «30» и «16», симметричных оси, — «Ø72»).
    weight = {
        id(line): _stroke(gray, ink, line, (line.start, line.end), axis=0, quantile=0.25)
        for line in lines
    }
    # Эталон толщины — 90-й процентиль толщин ДЛИННЫХ линий: короткие
    # горизонтали — это ещё и основания залитых стрелок размеров Ø (масса
    # 17–18 против 6 у кромки), и по всем линиям эталон отсекал сам контур.
    long_lines = [line for line in lines if line.end - line.start >= 4 * min_length]
    main_ref = float(np.percentile([weight[id(line)] for line in (long_lines or lines)], 90))
    main = [line for line in lines if weight[id(line)] >= _MAIN_SHARE * main_ref]
    vertical = _lines(ink, min_length, axis=1)
    context = _Sheet(gray=gray, ink=ink, main_weight=_MAIN_SHARE * main_ref)

    passing = []
    for axis_y in _axis_candidates(main, weight, min_length):
        found = _profile(main, axis_y, gray.shape[1], min_length)
        if found is None:
            continue
        x0, x1, half = found
        faces = _end_faces(vertical, axis_y, x0, x1, half, context)
        if faces is None:
            continue
        x0, x1 = faces
        if x1 - x0 < 4 * min_length:
            continue
        segment = _slice(half, x0, x1)
        passing.append((axis_y, x0, x1, segment, float(np.nansum(segment))))
    if not passing:
        return None
    primary = passing[0]
    reach = _SAME_VIEW_SHARE * (primary[2] - primary[1])
    same_shaft = [
        item
        for item in passing
        if abs(item[1] - primary[1]) <= reach and abs(item[2] - primary[2]) <= reach
    ]
    axis_y, x0, x1, segment, _area = max(same_shaft, key=lambda item: item[4])
    extent = float(np.nanmax(segment))
    # Грани уступов: основные вертикали внутри вида. Толщина — на отрезке
    # внутри профиля: грань тоже сливается с выносной цепочки размеров.
    inside = (axis_y - extent - 2.0, axis_y + extent + 2.0)
    faces = sorted(
        (_face_x(ink, line, inside), line.start, line.end)
        for line in vertical
        if x0 - 3 <= line.position <= x1 + 3
        and min(line.end, inside[1]) - max(line.start, inside[0]) >= min_length
        and _stroke(gray, ink, line, inside, axis=1) >= context.main_weight
    )
    profile = ShaftProfile(
        x0=x0, x1=x1, axis_y=axis_y, half_px=segment, faces_px=tuple(faces), line_px=main_ref
    )
    frame = ViewFrame(
        bbox_px=(x0 - 3, axis_y - extent - 3, x1 + 3, axis_y + extent + 3),
        mm_per_px=float(total_length_mm) / float(x1 - x0),
        origin_px=(float(x0), axis_y),
    )
    return frame, profile


def _segments(ink: Any, min_length: int) -> list[Any]:
    """Горизонтали вида: центр линии — по каждому столбцу, со скачком — разрез.

    Общий `_lines` утолщает маску по вертикали и берёт центр тяжести всей
    компоненты. Кромки соседних ступеней с близкими радиусами при этом
    сливаются (150 dpi: низ Ø30 и Ø28 в 4 px; 1:2 — Ø22 и Ø20 линиями в 6 px
    на 5,9 px), центр ложится между ними — пара пропадает, профиль рвётся или
    две ступени сливаются в одну «Ø21,4». Здесь без утолщения, а компонента
    режется там, где центр строки прыгает больше чем на пиксель.
    """
    import cv2
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _Line

    # Ядро размыкания (0,6 % меньшей стороны листа) на листах A-формата при
    # любом dpi примерно в 2,5 раза толще основной линии, и грани уступов им
    # стираются. На изображении, где ядро не толще линии, грань переживает
    # размыкание и сшивает верх и низ ступени — вычитать вертикали нельзя
    # (выносные режут кромки: корпус v7, чистый лист 26 → 12 из 28).
    opened = cv2.morphologyEx(
        ink.astype(np.uint8), cv2.MORPH_OPEN, np.ones((1, min_length), np.uint8)
    )
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(opened, connectivity=8)
    result = []
    for index in range(1, count):
        x, y, w, h, _area = stats[index]
        if w < min_length:
            continue
        block = labels[y : y + h, x : x + w] == index
        rows = np.arange(y, y + h, dtype=float)[:, None]
        counts = block.sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            centres = np.where(counts > 0, (block * rows).sum(axis=0) / counts, np.nan)
        start = 0
        for i in range(1, w + 1):
            if (
                i == w
                or np.isnan(centres[i])
                or np.isnan(centres[i - 1])
                or abs(centres[i] - centres[i - 1]) > 1.0
            ):
                piece = centres[start:i]
                if i - start >= min_length and not np.all(np.isnan(piece)):
                    result.append(
                        _Line(float(np.nanmean(piece)), float(x + start), float(x + i - 1))
                    )
                start = i
    return sorted(result, key=lambda line: line.position)


def _axis_candidates(lines: list[Any], weight: dict[int, float], min_length: int) -> list[float]:
    """Середины симметричных пар с наибольшим весом — лучшие первыми."""
    votes: dict[int, float] = {}
    for i, top in enumerate(lines):
        for bottom in lines[i + 1 :]:
            if bottom.position - top.position < 2 * _SYMMETRY_PX:
                continue
            overlap = top.overlap(bottom.start, bottom.end)
            if overlap < min_length:
                continue
            middle = int(round((top.position + bottom.position) / 2.0))
            votes[middle] = votes.get(middle, 0.0) + overlap * min(
                weight[id(top)], weight[id(bottom)]
            )
    smoothed = {y: sum(votes.get(y + d, 0.0) for d in (-1, 0, 1)) for y in votes}
    peaks = [
        y
        for y in smoothed
        if smoothed[y] >= smoothed.get(y - 1, 0.0) and smoothed[y] > smoothed.get(y + 1, 0.0)
    ]
    peaks.sort(key=lambda y: smoothed[y], reverse=True)
    result = []
    for y in peaks[:_AXIS_CANDIDATES]:
        near = [value for value in range(y - 1, y + 2) if value in votes]
        result.append(sum(value * votes[value] for value in near) / sum(votes[v] for v in near))
    return result


def _profile(lines: list[Any], axis_y: float, width: int, min_length: int):
    """Самая внешняя симметричная пара на каждом столбце, самый длинный участок."""
    import numpy as np

    half = np.full(width, np.nan)
    above = [line for line in lines if line.position < axis_y - _SYMMETRY_PX]
    below = [line for line in lines if line.position > axis_y + _SYMMETRY_PX]
    for top in above:
        mirror = 2.0 * axis_y - top.position
        for bottom in below:
            if abs(bottom.position - mirror) > _SYMMETRY_PX:
                continue
            start, end = max(top.start, bottom.start), min(top.end, bottom.end)
            if end - start < min_length:
                continue
            value = (bottom.position - top.position) / 2.0
            segment = half[int(start) : int(end) + 1]
            half[int(start) : int(end) + 1] = np.where(
                np.isnan(segment), value, np.maximum(segment, value)
            )
    columns = np.nonzero(~np.isnan(half))[0]
    if columns.size == 0:
        return None
    gap = max(3.0, _GAP_TOTAL_SHARE * float(columns[-1] - columns[0]))
    runs: list[list[int]] = [[int(columns[0]), int(columns[0])]]
    for x in columns[1:]:
        x = int(x)
        if x - runs[-1][1] <= gap:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    x0, x1 = max(runs, key=lambda run: run[1] - run[0])
    return x0, x1, half


def _end_faces(
    vertical: list[Any], axis_y: float, x0: int, x1: int, half: Any, sheet: _Sheet
) -> tuple[int, int] | None:
    """Торцы — основные вертикали через ось у обоих концов профиля; нет обоих — не ось.

    Вертикаль должна перекрывать большую часть высоты концевой ступени, а
    положение уточняется по самому внешнему прогону у оси (`_outer_x`).
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _stroke

    reach = max(3.0, 0.03 * (x1 - x0))
    extent = float(np.nanmax(half[x0 : x1 + 1]))
    near_axis = (axis_y - 0.25 * extent, axis_y + 0.25 * extent)
    edge = max(3, int(0.05 * (x1 - x0)))

    def end_height(lo: int, hi: int) -> float:
        values = half[max(0, lo) : max(0, hi) + 1]
        values = values[~np.isnan(values)]
        return float(np.median(values)) if values.size else extent

    left_height = end_height(x0, x0 + edge)
    right_height = end_height(x1 - edge, x1)

    def is_face(line: Any, height: float) -> bool:
        if not line.start <= axis_y <= line.end:
            return False
        # Торец идёт через большую часть высоты концевой ступени: короткий
        # обрывок через ось (метка, конец выноски) стал торцом у shaft-2 на
        # 150 dpi. Фаска укорачивает торец на свой размер — это меньше 40 %.
        span = min(line.end, axis_y + height) - max(line.start, axis_y - height)
        if span < 0.6 * 2.0 * height:
            return False
        weight = _stroke(sheet.gray, sheet.ink, line, near_axis, axis=1)
        return weight >= sheet.main_weight

    left = [
        line.position
        for line in vertical
        if x0 - reach <= line.position <= x0 + reach and is_face(line, left_height)
    ]
    right = [
        line.position
        for line in vertical
        if x1 - reach <= line.position <= x1 + reach and is_face(line, right_height)
    ]
    if not left or not right:
        return None
    band = 0.2 * min(left_height, right_height)
    rows = (axis_y - band, axis_y + band)
    x_left = _outer_x(sheet.ink, min(left), rows, reach, side=-1)
    x_right = _outer_x(sheet.ink, max(right), rows, reach, side=1)
    return int(round(x_left)), int(round(x_right))


def _outer_x(ink: Any, x: float, rows: tuple[float, float], reach: float, *, side: int) -> float:
    """Самый внешний прогон чернил у торца — по строкам у оси, медиана середин.

    По ЕСКД фаска на торце вала — ещё одна вертикаль через всю высоту на
    расстоянии фаски от торца; на 150 dpi она в 3 px от торца, линии сливаются
    в одну компоненту, и её центр уезжал внутрь (shaft-25: 858,9 вместо 862,2,
    shaft-7: 1077,5 вместо 1083,7 — масштаб вида мимо на 1,5 %). Окно — от
    найденной вертикали наружу на ширину окна торца.
    """
    import numpy as np

    width = ink.shape[1]
    if side > 0:
        lo, hi = max(0, int(x) - 2), min(width, int(x + reach) + 1)
    else:
        lo, hi = max(0, int(x - reach)), min(width, int(x) + 3)
    # Середины прогонов по строкам полосы у оси.
    samples: list[tuple[int, float]] = []
    inked_rows = 0
    for y in range(max(0, int(rows[0])), min(ink.shape[0], int(rows[1]) + 1)):
        columns = np.nonzero(ink[y, lo:hi])[0]
        if columns.size == 0:
            continue
        inked_rows += 1
        for run in np.split(columns, np.nonzero(np.diff(columns) > 1)[0] + 1):
            samples.append((y, (run[0] + run[-1]) / 2.0 + lo))
    if not samples:
        return float(x)
    # Край — вертикаль: один столбец почти во всех строках. Метку у торца
    # (shaft-2: окружность отверстия в 7 px от торца) выдаёт форма — у кривой
    # столбец меняется от строки к строке; правило «только вплотную» не
    # переносилось между разрешениями (фаска на 150 dpi — те же 4–7 px).
    samples.sort(key=lambda item: item[1])
    groups: list[list[tuple[int, float]]] = [[samples[0]]]
    for item in samples[1:]:
        if item[1] - groups[-1][-1][1] <= 1.5:
            groups[-1].append(item)
        else:
            groups.append([item])
    steady = [group for group in groups if len({row for row, _ in group}) >= 0.8 * inked_rows]
    if not steady:
        return float(x)
    edge = steady[-1] if side > 0 else steady[0]
    return float(np.median([column for _, column in edge]))


def _face_x(ink: Any, line: Any, inside: tuple[float, float]) -> float:
    """Столбец грани — середина линии в строках ВНУТРИ профиля, медиана по строкам.

    Вертикаль грани сливается в одну компоненту с выносной, стенкой канавки
    или размерной линией отверстия, и центр тяжести компоненты уезжал: грань
    на 250,0 выходила 251,3 (shaft-11), на 50 — 50,36 (shaft-16), хотя на
    листе она стоит ровно на станции. Внутри профиля грань стоит одна.
    """
    import numpy as np

    top = int(max(line.start, inside[0]))
    bottom = int(min(line.end, inside[1]))
    centre = int(round(line.position))
    reach = 6
    lo = max(0, centre - reach)
    hi = min(ink.shape[1], centre + reach + 1)
    middles = []
    for y in range(top, bottom + 1):
        cut = ink[y, lo:hi]
        columns = np.nonzero(cut)[0]
        if columns.size == 0:
            continue
        # Прогон чернил, ближайший к положению линии: соседняя вертикаль в
        # пределах окна не должна тянуть середину к себе.
        runs = np.split(columns, np.nonzero(np.diff(columns) > 1)[0] + 1)
        run = min(runs, key=lambda item: abs((item[0] + item[-1]) / 2.0 + lo - line.position))
        middles.append((run[0] + run[-1]) / 2.0 + lo)
    return float(np.median(middles)) if middles else float(line.position)


def _slice(half: Any, x0: int, x1: int) -> Any:
    import numpy as np

    out = np.full(x1 - x0 + 1, np.nan)
    lo, hi = max(0, x0), min(len(half) - 1, x1)
    out[lo - x0 : hi - x0 + 1] = half[lo : hi + 1]
    return out
