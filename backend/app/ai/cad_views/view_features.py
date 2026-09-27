"""Этап D3: элементы тела вращения по второму виду — отличия вида от профиля.

Вид сбоку тела вращения — силуэт его наружного профиля. Где силуэт уже
профиля, симметрично оси, — лыски: материал срезан с двух сторон до
ширины силуэта («Опора пружин»: хвостовик до 5 — вилка). Окружность внутри
силуэта с центром на оси — сквозное отверстие вдоль направления взгляда
(Ø5 и Ø1,5). Вид совмещается с профилем по торцам (проекционная связь).

Система детали: ось Z от левого торца, Y — направление взгляда этого вида
(в него уходят отверстия), X — поперёк вида (лыски срезают по X).
"""

from __future__ import annotations

import math
from typing import Any

from app.ai.cad_views.revolve_profile import HalfProfile


def _silhouette(ink: Any, axis_y: int, line: float) -> list[float | None]:
    import cv2
    import numpy as np

    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
    )
    half: list[float | None] = []
    for x in range(horizontal.shape[1]):
        edges = np.diff(np.concatenate([[0], horizontal[:, x], [0]]))
        # Только основные линии: размерные линии Ø тоже симметричны оси и
        # давали ложные «бурты» до 9 мм шириной (z4-r4: Ø37, Ø40, Ø44).
        centres = [
            (a + b - 1) / 2.0
            for a, b in zip(np.where(edges == 1)[0], np.where(edges == -1)[0])
            if b - a >= 0.6 * line
        ]
        up = [axis_y - c for c in centres if c < axis_y - line]
        down = [c - axis_y for c in centres if c > axis_y + line]
        pairs = [r for r in up if any(abs(r - q) <= 1.5 * line for q in down)]
        half.append(max(pairs) if pairs else None)
    return half


def _circles(
    ink: Any,
    line: float,
    *,
    threshold: int = 18,
    radius: tuple[float, float] | None = None,
    min_radius_lines: float = 3.0,
) -> list[tuple[float, float, float]]:
    """Окружности (центр x, y, радиус по середине штриха).

    Центры — Хафом; контуром нельзя: центровые линии перечёркивают
    окружность, и контур перестаёт быть замкнутым. Радиус Хафа неточен —
    уточняется по кольцу чернил: радиус, на котором окружность покрыта
    чернилами наибольшей долей (не меньше 0,7)."""
    import cv2
    import numpy as np

    image = ((1 - ink) * 255).astype(np.uint8)
    found = cv2.HoughCircles(
        cv2.GaussianBlur(image, (5, 5), 0),
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=3 * line,
        param1=120,
        param2=threshold,
        minRadius=int(radius[0]) if radius else int(1.5 * line),
        maxRadius=int(radius[1]) + 1 if radius else int(40 * line),
    )
    if found is None:
        return []
    angles = np.radians(np.arange(0, 360, 2))
    height, width = ink.shape
    out: list[tuple[float, float, float]] = []
    # С мягким порогом Хаф отдаёт тысячи кандидатов (по убыванию голосов):
    # лучшие 200 и уточнение радиуса в заданном окне — иначе минуты на лист.
    for cx, cy, r0 in found[0][:200]:
        low, high = (radius[0], radius[1]) if radius else (max(line, 0.5 * r0), 1.5 * r0 + line)
        radii = np.arange(low, high, 0.5)
        shares = []
        for r in radii:
            xs = np.clip(np.round(cx + r * np.cos(angles)).astype(int), 0, width - 1)
            ys = np.clip(np.round(cy + r * np.sin(angles)).astype(int), 0, height - 1)
            shares.append(float(ink[ys, xs].mean()))
        best = (0.0, None)
        if shares:
            top = max(shares)
            # Толстая обводка даёт плато максимума шириной в штрих — середина
            # плато и есть середина линии (первый максимум — внутренний край).
            peak = int(np.argmax(shares))
            left, right = peak, peak
            while left > 0 and shares[left - 1] >= top - 0.02:
                left -= 1
            while right < len(shares) - 1 and shares[right + 1] >= top - 0.02:
                right += 1
            best = (top, float(radii[left] + radii[right]) / 2.0)
        if best[1] is None or best[0] < 0.7 or best[1] < min_radius_lines * line:
            continue
        if any(
            math.hypot(cx - ox, cy - oy) < line and abs(best[1] - orad) < 2 * line
            for ox, oy, orad in out
        ):
            continue
        out.append((float(cx), float(cy), best[1]))
    return out


def snap(value: float, labels: list[float], share: float = 0.12) -> float:
    """Номинал — из надписи, если замер её объясняет (ближайшая в пределах доли)."""
    near = min(labels, key=lambda v: abs(v - value), default=None)
    return near if near is not None and abs(near - value) <= share * near else value


def side_view_features(
    view_gray: Any,
    profile: HalfProfile,
    axial_mm_per_px: float,
    radial_mm_per_px: float,
    diameter_labels: list[float] | None = None,
    linear_labels: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Элементы по размещению (как `placed_features` спека) по второму виду."""
    import numpy as np

    from app.ai.cad_views.section_material import ink_mask, symmetry_axis

    line = profile.line_px
    ink = ink_mask(view_gray, line)
    import cv2

    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
    )
    axis_y = symmetry_axis(horizontal)
    half = _silhouette(ink, axis_y, line)
    present = [x for x, h in enumerate(half) if h is not None]
    if not present:
        return []
    # Проекционная связь: вид в том же масштабе, что профиль, — ищется только
    # сдвиг вдоль оси, при котором силуэт лучше всего совпадает с профилем.
    xs_p = np.arange(profile.x0, profile.x1 + 1)
    expected = np.interp(xs_p, [x for x, _r in profile.outer], [r for _x, r in profile.outer])
    silhouette = np.array([np.nan if h is None else h for h in half], dtype=float)
    best = (np.inf, 0)
    for shift in range(-len(half), len(half)):
        cols = xs_p + shift
        inside = (cols >= 0) & (cols < len(half))
        if inside.sum() < 0.6 * len(xs_p):
            continue
        values = silhouette[cols[inside]]
        known = ~np.isnan(values)
        if known.sum() < 0.5 * len(xs_p):
            continue
        # Совпадение — доля столбцов, где силуэт равен профилю (лыски и
        # отверстия — отличия, их не штрафуем сверх доли).
        match = np.abs(values[known] - expected[inside][known]) <= 1.5 * line
        cost = -match.mean() * known.sum() / len(xs_p)
        if cost < best[0]:
            best = (cost, shift)
    shift = best[1]
    v0, v1 = profile.x0 + shift, profile.x1 + shift

    def z_of(x: float) -> float:
        return (x - v0) * axial_mm_per_px

    xs = [x for x, _r in profile.outer]
    rs = [r for _x, r in profile.outer]

    def outer_at(z: float) -> float:
        x = profile.x0 + z / axial_mm_per_px
        return float(np.interp(x, xs, rs)) * radial_mm_per_px

    features: list[dict[str, Any]] = []
    # Лыски: силуэт уже профиля на 0,2 мм и более, участок не короче 3 линий.
    tolerance = max(0.2, 2 * line * radial_mm_per_px)
    run: list[tuple[float, float]] = []

    def flush() -> None:
        if len(run) * 1.0 < 3 * line:
            run.clear()
            return
        z0, z1 = run[0][0], run[-1][0]
        width = snap(float(np.median([w for _z, w in run])), linear_labels or [])
        radius = max(outer_at(z) for z, _w in run)
        for sign in (1.0, -1.0):
            features.append(
                {
                    "kind": "pocket",
                    "profile": "rectangle",
                    "origin_mm": [sign * radius, 0.0, round((z0 + z1) / 2, 3)],
                    "axis": [-sign, 0.0, 0.0],
                    "ref": [0.0, 0.0, 1.0],
                    "width_mm": round(z1 - z0, 3),
                    "height_mm": round(2 * radius + 1.0, 3),
                    "depth_mm": round(radius - width / 2, 3),
                    "source": "лыски по второму виду",
                }
            )
        run.clear()

    for x in range(max(0, v0), min(len(half) - 1, v1) + 1):
        h = half[x]
        z = z_of(x)
        if h is None:
            continue
        width = 2 * h * radial_mm_per_px
        if width < 2 * outer_at(z) - tolerance:
            run.append((z, width))
        elif run:
            flush()
    if run:
        flush()
    # Отверстия: окружность в силуэте с центром на оси.
    # Мелкое отверстие (Ø1,5 «Опоры» — 19 px при линии 7,6) — от двух толщин.
    for cx, cy, r in _circles(ink, line, min_radius_lines=2.0):
        if not v0 <= cx <= v1 or abs(cy - axis_y) > max(2 * line, 0.1 * r):
            continue
        z = z_of(cx)
        diameter = 2 * r * radial_mm_per_px  # радиус уже по середине штриха
        if diameter <= 0 or diameter >= 2 * outer_at(z):
            continue
        features.append(
            {
                "kind": "hole",
                # Вход снаружи детали: сквозное строится от точки входа.
                "origin_mm": [0.0, round(max(rs) * radial_mm_per_px + 0.5, 3), round(z, 3)],
                "axis": [0.0, -1.0, 0.0],
                "ref": [0.0, 0.0, 1.0],
                "diameter_mm": round(snap(diameter, diameter_labels or []), 3),
                "through": True,
                "source": "окружность на втором виде",
            }
        )
    return features
