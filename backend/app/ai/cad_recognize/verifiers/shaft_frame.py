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
# Разрыв профиля, который ещё считается тем же видом: доля текущего куска
# или доля ширины листа (от одной доли куска профиль рвался в самом начале).
_GAP_SHARE = 0.03
_GAP_SHEET_SHARE = 0.005
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
    faces_px: tuple[float, ...] = ()

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
    lines = _lines(ink, min_length, axis=0)
    if len(lines) < 2:
        return None
    weight = {id(line): _stroke(gray, ink, line, (line.start, line.end), axis=0) for line in lines}
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
        line.position
        for line in vertical
        if x0 - 3 <= line.position <= x1 + 3
        and min(line.end, inside[1]) - max(line.start, inside[0]) >= min_length
        and _stroke(gray, ink, line, inside, axis=1) >= context.main_weight
    )
    profile = ShaftProfile(x0=x0, x1=x1, axis_y=axis_y, half_px=segment, faces_px=tuple(faces))
    frame = ViewFrame(
        bbox_px=(x0 - 3, axis_y - extent - 3, x1 + 3, axis_y + extent + 3),
        mm_per_px=float(total_length_mm) / float(x1 - x0),
        origin_px=(float(x0), axis_y),
    )
    return frame, profile


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
    floor = max(3.0, _GAP_SHEET_SHARE * width)
    runs: list[list[int]] = [[int(columns[0]), int(columns[0])]]
    for x in columns[1:]:
        x = int(x)
        span = runs[-1][1] - runs[-1][0] + 1
        if x - runs[-1][1] <= max(floor, _GAP_SHARE * span):
            runs[-1][1] = x
        else:
            runs.append([x, x])
    x0, x1 = max(runs, key=lambda run: run[1] - run[0])
    return x0, x1, half


def _end_faces(
    vertical: list[Any], axis_y: float, x0: int, x1: int, half: Any, sheet: _Sheet
) -> tuple[int, int] | None:
    """Торцы — основные вертикали через ось у обоих концов профиля; нет обоих — не ось."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _stroke

    reach = max(3.0, 0.03 * (x1 - x0))
    extent = float(np.nanmax(half[x0 : x1 + 1]))
    near_axis = (axis_y - 0.25 * extent, axis_y + 0.25 * extent)

    def is_face(line: Any) -> bool:
        if not line.start <= axis_y <= line.end:
            return False
        weight = _stroke(sheet.gray, sheet.ink, line, near_axis, axis=1)
        return weight >= sheet.main_weight

    left = [
        line.position
        for line in vertical
        if x0 - reach <= line.position <= x0 + reach and is_face(line)
    ]
    right = [
        line.position
        for line in vertical
        if x1 - reach <= line.position <= x1 + reach and is_face(line)
    ]
    if not left or not right:
        return None
    return int(round(min(left))), int(round(max(right)))


def _slice(half: Any, x0: int, x1: int) -> Any:
    import numpy as np

    out = np.full(x1 - x0 + 1, np.nan)
    lo, hi = max(0, x0), min(len(half) - 1, x1)
    out[lo - x0 : hi - x0 + 1] = half[lo : hi + 1]
    return out
