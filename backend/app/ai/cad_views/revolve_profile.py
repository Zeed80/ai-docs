"""Этап D1: полупрофиль тела вращения по изображению — как читает инженер.

На виде или разрезе тела вращения каждая основная линия контура имеет
зеркальную пару относительно оси. Идя вдоль оси, в каждом столбце берутся
симметричные пары горизонтальных штрихов: самая дальняя — наружный контур,
следующая за ней — стенка расточки (на разрезе). Штриховка под 45° и
вертикальные размерные линии снимаются горизонтальным размыканием; класс
детали не нужен — годится для вала, втулки, полой детали с конусом и дугой
(«Опора пружин»: 5 ступеней, шейка по дуге, конус 18°, трубка).

Выход в пикселях изображения; в миллиметры его переводит этап C (надписи Ø).

Прототип (2026-09-26): на «Опоре пружин» шейка, конус, уступы и трубка
находятся верно, тонкая стенка со штриховкой вплотную к кромке (левое кольцо)
— нет: столбцовый разбор путает кромку со штрихом. Надёжный путь — по
векторному контуру вида (этап B1), этот модуль станет его потребителем.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HalfProfile:
    axis_y: float
    line_px: float
    x0: int
    x1: int
    outer: list[tuple[float, float]] = field(default_factory=list)  # (x, r) ломаная
    inner: list[tuple[float, float]] = field(default_factory=list)  # пусто — сплошное


def _ink(gray: Any) -> Any:
    import cv2
    import numpy as np

    g = np.asarray(gray)
    background = cv2.medianBlur(g, 31).astype(float)
    return ((background - g) > 35).astype(np.uint8)


def _line_px(ink: Any) -> float:
    import numpy as np

    runs: list[int] = []
    for x in range(0, ink.shape[1], 3):
        edges = np.diff(np.concatenate([[0], ink[:, x], [0]]))
        runs.extend(int(r) for r in np.where(edges == -1)[0] - np.where(edges == 1)[0] if r < 40)
    return float(np.median(runs)) if runs else 3.0


def _axis(horizontal: Any) -> int:
    """Строка наибольшей зеркальной симметрии горизонтальных штрихов."""
    height = horizontal.shape[0]
    best = (-1.0, height // 2)
    for y in range(height // 5, 4 * height // 5):
        half = min(y, height - y)
        above = horizontal[y - half : y].astype(float)
        below = horizontal[y : y + half][::-1].astype(float)
        score = float((above * below).sum()) / (float(above.sum() + below.sum()) + 1.0)
        if score > best[0]:
            best = (score, y)
    return best[1]


# Основная линия — не тоньше этой доли медианной толщины штрихов.
_MAIN_SHARE = 0.6


def _despike(values: list[float | None], width: int) -> list[float | None]:
    """Уровни короче ``width`` столбцов между двумя одинаковыми соседями —
    стыки граней и пересечения со штриховкой, а не ступени: заменяются соседом."""
    out = list(values)
    runs: list[list] = []  # [start, end, level]
    for x, v in enumerate(out):
        if runs and v is not None and runs[-1][2] is not None and abs(v - runs[-1][2]) <= 1.5:
            runs[-1][1] = x
        elif runs and v is None and runs[-1][2] is None:
            runs[-1][1] = x
        else:
            runs.append([x, x, v])
    for i in range(1, len(runs) - 1):
        start, end, _level = runs[i]
        before, after = runs[i - 1][2], runs[i + 1][2]
        if end - start + 1 < width and before is not None and after is not None:
            if abs(before - after) <= 2.0 or runs[i][2] is None:
                for x in range(start, end + 1):
                    out[x] = before if abs(before - after) <= 2.0 else None
    return out


def _simplify(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    import cv2
    import numpy as np

    if len(points) < 3:
        return points
    curve = np.array(points, dtype=np.float32).reshape(-1, 1, 2)
    return [(float(p[0][0]), float(p[0][1])) for p in cv2.approxPolyDP(curve, tolerance, False)]


def half_profile(gray: Any) -> HalfProfile | None:
    """Полупрофиль по изображению тела вращения (ось горизонтальна)."""
    import cv2
    import numpy as np

    ink = _ink(gray)
    line = _line_px(ink)
    step = max(3, int(round(3 * line)))
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, step), np.uint8))
    axis_y = _axis(horizontal)
    outer: list[float | None] = []
    inner: list[float | None] = []
    for x in range(horizontal.shape[1]):
        edges = np.diff(np.concatenate([[0], horizontal[:, x], [0]]))
        # Только основные линии: тонкие выносные продолжают контур за торцом
        # на том же радиусе («Опора пружин»: трубка и её выносная справа).
        centres = [
            (a + b - 1) / 2.0
            for a, b in zip(np.where(edges == 1)[0], np.where(edges == -1)[0])
            if b - a >= _MAIN_SHARE * line
        ]
        up = sorted(axis_y - c for c in centres if c < axis_y - line)
        down = [c - axis_y for c in centres if c > axis_y + line]
        pairs = [r for r in up if any(abs(r - q) <= 1.5 * line for q in down)]
        outer.append(max(pairs) if pairs else None)
        inner.append(pairs[-2] if len(pairs) > 1 else None)
    outer = _despike(outer, int(2 * line))
    inner = _despike(inner, int(2 * line))
    # Деталь — самый длинный непрерывный участок наружного контура: выносные
    # и размерные линии за торцами рвутся от него разрывами.
    gap = max(4, int(4 * line))
    runs: list[list[int]] = []
    for x, r in enumerate(outer):
        if r is None:
            continue
        if runs and x - runs[-1][1] <= gap:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    if not runs:
        return None
    x0, x1 = max(runs, key=lambda run: run[1] - run[0])
    tolerance = max(1.0, 0.5 * line)
    outer_pts = [(float(x), float(outer[x])) for x in range(x0, x1 + 1) if outer[x] is not None]
    inner_pts = [(float(x), float(inner[x])) for x in range(x0, x1 + 1) if inner[x] is not None]
    return HalfProfile(
        axis_y=float(axis_y),
        line_px=line,
        x0=int(x0),
        x1=int(x1),
        outer=_simplify(outer_pts, tolerance),
        inner=_simplify(inner_pts, tolerance) if len(inner_pts) > 0.3 * (x1 - x0) else [],
    )
