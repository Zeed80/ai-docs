"""Размерные линии вдоль оси тела вращения — по геометрии листа, без текста.

Надпись говорит «сколько», а какую пару станций она мерит — говорит место её
размерной линии. Без него на листе не в масштабе надпись привязывается к
ближайшему по длине звену и промахивается (шпиндель 793539cc_p013: «10» от
торца нарисовано 14 мм, а уступ за канавкой — ровно 10 мм от неё). Размерная
линия — тонкая горизонталь вне контура со стрелками на концах; её концы
лежат на выносных линиях, которые идут от контура детали. Пара выносных —
пара станций.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class AxialSpan:
    """Размер вдоль оси: столбцы двух выносных (px листа) и строка линии."""

    a: float
    b: float
    row: float

    @property
    def length(self) -> float:
        return self.b - self.a


def _runs(mask: Any) -> list[tuple[int, int]]:
    import numpy as np

    edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.where(edges == 1)[0].tolist(), (np.where(edges == -1)[0] - 1).tolist()))


def axial_spans(
    gray: Any,
    x0: float,
    x1: float,
    axis: float,
    radius_at: Callable[[float], float],
    line: float,
    reach: float,
) -> list[AxialSpan]:
    """Размерные линии вдоль горизонтальной оси вида (координаты листа).

    ``radius_at(x)`` — радиус контура детали в столбце x (px), ``reach`` —
    насколько далеко от оси искать ряды размеров (px)."""
    import cv2
    import numpy as np

    from app.ai.cad_views.section_material import ink_mask

    g = np.asarray(gray)
    height, width = g.shape[:2]
    pad = 0.08 * (x1 - x0) + 10 * line
    cx0, cx1 = int(max(0, x0 - pad)), int(min(width, x1 + pad))
    cy0, cy1 = int(max(0, axis - reach)), int(min(height, axis + reach))
    if cx1 - cx0 < 10 or cy1 - cy0 < 10:
        return []
    ink = ink_mask(g[cy0:cy1, cx0:cx1], line).astype(np.uint8)
    step = max(2.0, line)
    long = max(9, int(round(5 * step)))
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, long), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((long, 1), np.uint8))
    a_axis = axis - cy0

    def outline_r(column: float) -> float:
        # Радиус у столбца — наибольший в окрестности: выносная у уступа
        # начинается от большей ступени.
        x = column + cx0
        window = 2 * step
        return max(radius_at(x - window), radius_at(x), radius_at(x + window))

    # Выносные — вертикали, ближний к оси конец которых у контура детали.
    count, _labels, stats, _c = cv2.connectedComponentsWithStats(vertical, 8)
    witnesses: list[tuple[float, int, int]] = []
    for index in range(1, count):
        left, top, w, h, _area = (int(v) for v in stats[index])
        if w > 3 * step:
            continue
        column = left + (w - 1) / 2.0
        near = 0.0 if top <= a_axis <= top + h else min(abs(top - a_axis), abs(top + h - a_axis))
        r = outline_r(column)
        if r <= 0 or near > r + 4 * step:
            continue
        witnesses.append((column, top, top + h - 1))

    # Размерные линии — горизонтали вне контура.
    count, labels_h, stats, _c = cv2.connectedComponentsWithStats(horizontal, 8)
    spans: list[AxialSpan] = []
    for index in range(1, count):
        left, top, w, h, _area = (int(v) for v in stats[index])
        if h > 3 * step:
            continue
        # Строка линии — по средней половине: стрелки на концах утолщают
        # компоненту, и её середина уходит со штриха.
        middle = labels_h[top : top + h, left + w // 4 : left + 3 * w // 4 + 1] == index
        # Строка с наибольшим заполнением: низ цифр надписи касается линии.
        filled = middle.sum(axis=1)
        best = np.nonzero(filled >= 0.8 * filled.max())[0] if filled.size and filled.max() else []
        row = top + float(np.mean(best)) if len(best) else top + (h - 1) / 2.0
        columns = np.arange(left, left + w, max(1, w // 40))
        if any(abs(row - a_axis) <= outline_r(float(c)) + 1.5 * step for c in columns):
            continue
        crossing = sorted(
            column
            for column, w_top, w_bottom in witnesses
            if left - 2 * step <= column <= left + w - 1 + 2 * step
            and w_top - step <= row <= w_bottom + step
        )
        merged: list[float] = []
        for column in crossing:
            if merged and column - merged[-1] <= 2 * step:
                continue
            merged.append(column)
        # Толщина самой размерной линии — по середине горизонтали (стрелки
        # на концах её утолщают): стрелка — штрих заметно толще этой линии.
        thin = []
        for c in columns[len(columns) // 4 : 3 * len(columns) // 4 + 1]:
            r0 = int(max(0, row - 2 * step))
            for s, e in _runs(ink[r0 : int(row + 2 * step) + 1, int(c)] > 0):
                if s + r0 <= row + 1 and e + r0 >= row - 1:
                    thin.append(e - s + 1)
        # Нижняя четверть: надпись над линией местами сливается с ней.
        width_px = float(np.percentile(thin, 25)) if thin else 1.0
        # Концы размера — выносные со стрелкой на этой линии; чужая выносная,
        # пересекающая линию без стрелки, размер не делит (цепочка в один ряд
        # — делит: стрелки у каждой выносной).
        arrowed = [c for c in merged if _arrow(ink, c, row, +1, step, width_px)]
        for a, b in zip(arrowed, arrowed[1:]):
            if b - a >= 2 * step:
                spans.append(AxialSpan(a + cx0, b + cx0, row + cy0))
    return spans


def _arrow(ink: Any, column: float, row: float, inward: int, step: float, width_px: float) -> bool:
    """Стрелка у конца размерной линии — внутри или снаружи пары выносных:
    рядом с концом штрих выше и ниже линии толще самой линии."""
    import numpy as np

    height, width = ink.shape
    r0, r1 = int(max(0, row - 4 * step)), int(min(height, row + 4 * step + 1))
    for direction in (inward, -inward):
        thickest = 0
        for offset in np.arange(1.5 * step, 6 * step, 1.0):
            x = int(round(column + direction * offset))
            if not 0 <= x < width:
                break
            runs = [
                (s, e)
                for s, e in _runs(ink[r0:r1, x] > 0)
                if s + r0 <= row + step and e + r0 >= row - step
            ]
            if runs:
                thickest = max(thickest, max(e - s + 1 for s, e in runs))
        if thickest >= max(3.0, 2.5 * width_px):
            return True
    return False


def match_spans(
    spans: list[tuple[float, float]],
    labels: list[float],
    scale: float | None = None,
    spread: float = 2.0,
) -> list[tuple[float, float, float]]:
    """Надписи — размерным линиям по порядку длин: [(a, b, надпись)].

    Лист не в масштабе искажает пропорции, но не порядок: размер больше —
    линия длиннее (шпиндель: 33, 135, 152, 226, 809, 1200 px — «2», «10»,
    «14», «22», «85», «127»). Сопоставление — монотонное с пропусками:
    наибольшее число пар при наименьшем отклонении от общего масштаба
    (наибольшая надпись — наибольшая линия, или заданный ``scale`` мм/px),
    не дальше чем в ``spread`` раз."""
    import math

    ordered = sorted(spans, key=lambda s: s[1] - s[0])
    values = sorted(v for v in labels if v > 0)
    if not ordered or not values:
        return []
    if scale is None:
        scale = values[-1] / max(1e-9, ordered[-1][1] - ordered[-1][0])
    n, m = len(ordered), len(values)
    # best[i][j] — (число пар, −отклонение) для первых i линий и j надписей.
    best = [[(0, 0.0)] * (m + 1) for _ in range(n + 1)]
    move = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            options = [(best[i - 1][j], 1), (best[i][j - 1], 2)]
            px = ordered[i - 1][1] - ordered[i - 1][0]
            ratio = values[j - 1] / max(1e-9, px * scale)
            if 1.0 / spread <= ratio <= spread:
                count, cost = best[i - 1][j - 1]
                options.append(((count + 1, cost - abs(math.log(ratio))), 3))
            best[i][j], move[i][j] = max(options, key=lambda o: o[0])
    out = []
    i, j = n, m
    while i > 0 and j > 0:
        if move[i][j] == 3:
            out.append((ordered[i - 1][0], ordered[i - 1][1], values[j - 1]))
            i, j = i - 1, j - 1
        elif move[i][j] == 1:
            i -= 1
        else:
            j -= 1
    return out[::-1]


def stations_from_spans(
    stations: list[float],
    matched: list[tuple[float, float, float]],
    tolerance: float,
    interpolate: bool = True,
) -> dict[float, float]:
    """{станция px: номинал мм} — от левого торца по размерам, привязанным к
    парам станций. Конец размера дальше ``tolerance`` от станций — размер не
    профиля (длина паза), он пропускается. Станции без размера — между
    соседними известными пропорционально (``interpolate``); порядок вдоль оси обязан
    сохраниться, иначе — пусто."""
    raw = sorted(set(stations))
    if len(raw) < 2:
        return {}
    # Точки одной станции (уступ замером — две близкие точки, край канавки
    # у уступа) — одна станция: иначе два размера к одному уступу ложились
    # на разные точки и разводили его (shaft-4: 19,86 → 15 при 20 → 20).
    groups: list[list[float]] = []
    for x in raw:
        if groups and x - groups[-1][-1] <= tolerance:
            groups[-1].append(x)
        else:
            groups.append([x])
    points = [g[0] for g in groups]
    member = {x: g[0] for g in groups for x in g}

    def nearest(x: float) -> float | None:
        best = min(raw, key=lambda p: abs(p - x))
        return member[best] if abs(best - x) <= tolerance else None

    edges = []
    for a, b, value in matched:
        pa, pb = nearest(a), nearest(b)
        if pa is None or pb is None or pa == pb:
            continue
        edges.append((min(pa, pb), max(pa, pb), value))
    known: dict[float, float] = {points[0]: 0.0}
    conflicts: set[tuple[float, float, float]] = set()
    edges.sort(key=lambda e: -e[2])
    changed = True
    while changed:
        changed = False
        for a, b, value in edges:
            if a in known and b not in known:
                known[b] = known[a] + value
                changed = True
            elif b in known and a not in known:
                known[a] = known[b] - value
                changed = True
            elif a in known and b in known and abs(known[b] - known[a] - value) > 0.01 * value:
                conflicts.add((a, b, value))
    # Противоречащих размеров много — привязка по порядку длин неверна.
    if len(known) < 2 or 3 * len(conflicts) > len(edges):
        return {}
    fixed = sorted(known)
    if any(known[q] <= known[p] for p, q in zip(fixed, fixed[1:])):
        return {}
    out = dict(known)
    for x in points if interpolate else []:
        if x in out:
            continue
        left = [p for p in fixed if p < x]
        right = [p for p in fixed if p > x]
        if left and right:
            p, q = left[-1], right[0]
            out[x] = known[p] + (known[q] - known[p]) * (x - p) / (q - p)
    return {x: out[member[x]] for x in raw if member[x] in out}
