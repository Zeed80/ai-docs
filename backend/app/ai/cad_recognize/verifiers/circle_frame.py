"""Система координат плана круглой детали (фланца) — по самому листу.

Контур фланца на плане — самая большая замкнутая окружность вида; её центр —
начало отсчёта (как у спека: центры отверстий от оси), а прочитанный
наружный Ø даёт масштаб. Окружность центров отверстий тоже замкнута и
концентрична, но меньше; дуги скруглений и выносные не замкнуты.

Hough — на уменьшенном листе (крупные радиусы, быстро), уточнение и
замкнутость — по секторам на исходном (`plate_hole._fit_circle`).
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Доля секторов, покрытых линией, чтобы окружность считалась замкнутой:
# контур пересекают размерные линии и выноски — это несколько секторов.
_MIN_COVERAGE = 0.8
# Радиус контура в долях меньшей стороны листа.
_RADIUS_RANGE = (0.06, 0.5)
_WORK_SIDE = 1000


def locate_circle_frame(sheet: Any, diameter_mm: float) -> ViewFrame | None:
    """Окружность контура на листе → вид с началом в её центре; ``None`` — нет."""
    import cv2
    import numpy as np

    if not diameter_mm or diameter_mm <= 0:
        return None
    gray = np.ascontiguousarray(np.asarray(sheet), dtype=np.uint8)
    factor = min(1.0, _WORK_SIDE / max(gray.shape))
    small = cv2.resize(gray, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    side = min(small.shape)
    found = cv2.HoughCircles(
        cv2.GaussianBlur(small, (0, 0), 1.2),
        cv2.HOUGH_GRADIENT,
        dp=1.0,
        minDist=max(8, int(0.02 * side)),
        param1=100,
        param2=max(20, int(0.05 * side)),
        minRadius=int(_RADIUS_RANGE[0] * side),
        maxRadius=int(_RADIUS_RANGE[1] * side),
    )
    if found is None:
        return None
    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    ink = _ink(gray)
    best = None
    for u, v, r in found[0][:40]:
        # Уточнение — в окне вокруг кандидата на исходном листе.
        cu, cv_, cr = u / factor, v / factor, r / factor
        pad = int(cr * 1.3) + 4
        x0, y0 = max(0, int(cu) - pad), max(0, int(cv_) - pad)
        x1, y1 = min(gray.shape[1], int(cu) + pad), min(gray.shape[0], int(cv_) + pad)
        # Уточнение контура — СВОИМ узким кольцом: у `_fit_circle` оно в
        # четверть радиуса и тянуло контур к окружности центров и отверстиям
        # внутри (flange-17: радиус −20 px, узкая замкнутость 0,01).
        fitted = _refine(ink[y0:y1, x0:x1], cu - x0, cv_ - y0, cr)
        if fitted is None:
            continue
        fu, fv, fr = fitted[0] + x0, fitted[1] + y0, fitted[2]
        # Замкнутость — узкой полосой вокруг УТОЧНЁННОЙ окружности. Кольцо
        # подгонки шириной в четверть радиуса у контура в 800 px — полоса в
        # 200 px: она собирает все линии вида, и ложная окружность выходила
        # «замкнутой» (flange-0: центр мимо на 380 px, масштаб мимо на 13 %).
        if _tight_coverage(ink, fu, fv, fr) < _MIN_COVERAGE:
            continue
        if best is None or fr > best[2]:
            best = (fu, fv, fr)
    if best is None:
        return None
    cx, cy, radius = best
    # Центр любой замкнутой окружности вида верен (все концентричны), а вот
    # самой большой среди кандидатов Hough контура часто нет вовсе: на 10 из
    # 24 фланцев выигрывала окружность центров или расточка (радиус 166–647
    # вместо 472–738 px), хотя узкая замкнутость на настоящем контуре — 1,00.
    # Поэтому радиус контура ищется от центра: наибольший радиус, окружность
    # которого замкнута линией.
    outer = _largest_closed_radius(ink, cx, cy)
    if outer is not None and outer > radius:
        refined = _refine(ink, cx, cy, outer)
        if refined is not None and _tight_coverage(ink, *refined) >= _MIN_COVERAGE:
            cx, cy, radius = refined
    return _frame(cx, cy, radius, diameter_mm)


def _largest_closed_radius(ink: Any, cx: float, cy: float) -> float | None:
    """Наибольший радиус вокруг центра, где окружность покрыта чернилами.

    Полярная развёртка: для каждого радиуса — доля из 360 направлений, где на
    окружности (± узкая полоса) есть линия. Радиальные размерные линии и
    выноски дают по одному-два направления и замкнутости не создают.
    """
    import numpy as np

    height, width = ink.shape
    limit = int(min(cx, cy, width - 1 - cx, height - 1 - cy))
    if limit < 10:
        return None
    radii = np.arange(1, limit + 1, dtype=float)
    angles = np.linspace(0.0, 2.0 * np.pi, 360, endpoint=False)[:, None]
    xs = np.rint(cx + radii[None, :] * np.cos(angles)).astype(int)
    ys = np.rint(cy + radii[None, :] * np.sin(angles)).astype(int)
    polar = ink[ys, xs]
    # Узкая полоса по радиусу: линия толщиной в пару пикселей и округление.
    # Сдвиг — без заворота: `np.roll` приносил на краевые радиусы плотные
    # чернила у самого центра, и «контуром» становился край листа.
    band = polar.copy()
    for shift in (1, 2):
        band[:, shift:] |= polar[:, :-shift]
        band[:, :-shift] |= polar[:, shift:]
    coverage = band.mean(axis=0)
    closed = np.nonzero(coverage >= _MIN_COVERAGE)[0]
    return float(radii[closed[-1]]) if closed.size else None


_SECTORS = 72


def _refine(ink: Any, u: float, v: float, r: float) -> tuple[float, float, float] | None:
    """Окружность по чернилам в сужающемся кольце: ±6 % радиуса, затем ±1,5 %.

    В каждом секторе — медианное расстояние чернил до центра, по этим точкам —
    окружность (Kåsa) с отбросом далёких секторов.
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_hole import _kasa

    ys, xs = np.nonzero(ink)
    if xs.size < 12:
        return None
    for width in (0.06 * r, max(2.0, 0.015 * r)):
        distance = np.hypot(xs - u, ys - v)
        ring = np.abs(distance - r) <= width
        if int(ring.sum()) < 12:
            return None
        angles = np.arctan2(ys[ring] - v, xs[ring] - u)
        sector = ((angles + np.pi) / (2 * np.pi) * _SECTORS).astype(int) % _SECTORS
        radii = distance[ring]
        points = []
        for index in range(_SECTORS):
            chosen = radii[sector == index]
            if chosen.size:
                angle = (index + 0.5) / _SECTORS * 2 * np.pi - np.pi
                rho = float(np.median(chosen))
                points.append((u + rho * np.cos(angle), v + rho * np.sin(angle)))
        if len(points) < 8:
            return None
        pts = np.asarray(points)
        u, v, r = _kasa(pts)
        deviation = np.abs(np.hypot(pts[:, 0] - u, pts[:, 1] - v) - r)
        keep = deviation <= max(1.0, 3.0 * float(np.median(deviation)))
        if int(keep.sum()) >= 8:
            u, v, r = _kasa(pts[keep])
        if not np.isfinite(r) or r <= 0:
            return None
    return float(u), float(v), float(r)


def _tight_coverage(ink: Any, cx: float, cy: float, radius: float) -> float:
    """Доля из 360 направлений, где на окружности (± узкая полоса) есть чернила."""
    import numpy as np

    band = max(2.0, 0.006 * radius)
    angles = np.linspace(0.0, 2.0 * np.pi, 360, endpoint=False)[:, None]
    offsets = np.arange(-band, band + 0.5, 1.0)[None, :]
    xs = np.rint(cx + (radius + offsets) * np.cos(angles)).astype(int)
    ys = np.rint(cy + (radius + offsets) * np.sin(angles)).astype(int)
    inside = (xs >= 0) & (ys >= 0) & (xs < ink.shape[1]) & (ys < ink.shape[0])
    hit = np.zeros(xs.shape, dtype=bool)
    hit[inside] = ink[ys[inside], xs[inside]]
    return float(hit.any(axis=1).mean())


def _frame(cx: float, cy: float, radius: float, diameter_mm: float) -> ViewFrame:
    return ViewFrame(
        bbox_px=(cx - radius - 3, cy - radius - 3, cx + radius + 3, cy + radius + 3),
        mm_per_px=diameter_mm / (2.0 * radius),
        origin_px=(cx, cy),
    )
