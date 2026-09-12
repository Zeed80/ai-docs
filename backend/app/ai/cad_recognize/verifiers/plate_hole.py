"""Проверяльщик отверстия пластины: окружность на заявленном x — её y и Ø.

Базовая линия ридера (plate-1, лист с исправленными подписями): все четыре x
отверстий прочитаны точно, а y переставлены парами и два Ø мимо. На листе
одинаковые координаты стоят один раз, и связать выносную со своим отверстием
модели трудно — это работа проверяльщика, а не промпта.

Поэтому поиск не вокруг заявленной точки, а в вертикальной ПОЛОСЕ на
заявленном x по всей высоте вида: прочитанный x выбирает окружность, её центр
и диаметр меряются. Окружность — Hough в полосе с радиусом у ожидаемого
(в `spec_crosscheck` записано, почему Hough по всему листу не годится: либо
не видит настоящее отверстие, либо находит несуществующие), затем уточнение
по чернилам в кольце вокруг найденной окружности — выносные и диаметральная
линия, идущие через отверстие, в кольцо почти не попадают.
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.registry import register
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Допуски сравнения прочитанного с измеренным, мм детали: соседние Ø по ряду
# ГОСТ отличаются минимум на 0,5, координаты на листе — целые или десятые.
_POSITION_MM = 0.5
_DIAMETER_MM = 0.3
# Во сколько раз настоящее отверстие может быть меньше или больше прочитанного.
_RADIUS_SPAN = (0.4, 2.5)


@register("plate_hole", min_feature_px=8.0)
def verify_plate_hole(hypothesis: Hypothesis, frame: ViewFrame | None, sheet: Any) -> Verdict:
    """``sheet`` — серое изображение листа (numpy, 0 — чернила, 255 — бумага)."""
    import numpy as np

    if frame is None:
        return Verdict(status="unmeasurable", reason="нет системы координат вида")
    x_mm = hypothesis.expected.get("x_mm")
    y_mm = hypothesis.expected.get("y_mm")
    diameter_mm = hypothesis.expected.get("diameter_mm")
    if x_mm is None or diameter_mm is None:
        return Verdict(status="unmeasurable", reason="нет прочитанного x или Ø")
    gray = np.asarray(sheet)
    radius_px = diameter_mm / 2.0 / frame.mm_per_px
    cx, _cy = frame.to_px(x_mm, y_mm if y_mm is not None else 0.0)
    x0b, y0b, x1b, y1b = (int(round(v)) for v in frame.bbox_px)
    # Прочитанному Ø верить нельзя (plate-1: Ø11 прочитан как 6,5), опора —
    # только x: полоса и диапазон радиусов берут отверстие до 2,5 раз больше.
    half = int(round(_RADIUS_SPAN[1] * radius_px + _POSITION_MM / frame.mm_per_px)) + 2
    left, right = max(x0b, int(cx) - half), min(x1b, int(cx) + half)
    top, bottom = max(y0b, 0), min(y1b, gray.shape[0])
    if right - left < 2 * radius_px or bottom - top < 2 * radius_px:
        return Verdict(status="unmeasurable", reason="полоса поиска вне вида")
    strip = gray[top:bottom, left:right]
    circles = _hough(strip, radius_px)
    if not circles:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=(left, top, right, bottom),
            reason="на заявленном x окружности нет",
        )
    # Прочитанный x выбирает окружность: из тех, чей центр на этом x, —
    # ближайшая к прочитанному y (если его нет — к середине полосы).
    target_y = frame.to_px(x_mm, y_mm)[1] - top if y_mm is not None else strip.shape[0] / 2.0
    tolerance_px = max(2.0, _POSITION_MM / frame.mm_per_px)
    on_column = [c for c in circles if abs(c[0] + left - cx) <= tolerance_px + 0.35 * radius_px]
    if not on_column:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=(left, top, right, bottom),
            reason="окружности в полосе есть, но не на заявленном x",
        )
    u, v, r = min(on_column, key=lambda c: abs(c[1] - target_y))
    u, v, r = _refine(strip, u, v, r)
    centre_px = (u + left, v + top)
    measured_x, measured_y = frame.to_mm(*centre_px)
    measured = {
        "x_mm": round(measured_x, 3),
        "y_mm": round(measured_y, 3),
        "diameter_mm": round(2.0 * r * frame.mm_per_px, 3),
    }
    bbox = (centre_px[0] - r, centre_px[1] - r, centre_px[0] + r, centre_px[1] + r)
    problems = []
    if abs(measured["x_mm"] - x_mm) > _POSITION_MM:
        problems.append(f"x {measured['x_mm']:g} мм, прочитано {x_mm:g}")
    if y_mm is not None and abs(measured["y_mm"] - y_mm) > _POSITION_MM:
        problems.append(f"y {measured['y_mm']:g} мм, прочитано {y_mm:g}")
    if abs(measured["diameter_mm"] - diameter_mm) > _DIAMETER_MM:
        problems.append(f"Ø {measured['diameter_mm']:g} мм, прочитано {diameter_mm:g}")
    return Verdict(
        status="refuted" if problems else "confirmed",
        measured=measured,
        evidence_bbox_px=bbox,
        anchors_px=(centre_px,),
        reason="; ".join(problems),
    )


def _hough(strip: Any, radius_px: float) -> list[tuple[float, float, float]]:
    """Окружности радиуса около ожидаемого в полосе: ``(u, v, r)`` в пикселях полосы."""
    import cv2
    import numpy as np

    image = np.ascontiguousarray(strip, dtype=np.uint8)
    blurred = cv2.GaussianBlur(image, (0, 0), max(0.8, radius_px / 25.0))
    votes = max(8, int(round(0.9 * radius_px)))
    found = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.0,
        minDist=max(4, int(radius_px)),
        param1=100,
        param2=votes,
        minRadius=max(3, int(radius_px * _RADIUS_SPAN[0])),
        maxRadius=max(5, int(radius_px * _RADIUS_SPAN[1])),
    )
    if found is None:
        return []
    return [(float(c[0]), float(c[1]), float(c[2])) for c in found[0]]


def _refine(strip: Any, u: float, v: float, r: float) -> tuple[float, float, float]:
    """Центр и радиус по чернилам в кольце вокруг найденной окружности (подгонка Kåsa)."""
    import numpy as np

    ink = np.asarray(strip) < 160
    ys, xs = np.nonzero(ink)
    if xs.size < 12:
        return u, v, r
    distance = np.hypot(xs - u, ys - v)
    ring = np.abs(distance - r) <= max(1.5, 0.2 * r)
    if int(ring.sum()) < 12:
        return u, v, r
    x, y = xs[ring].astype(float), ys[ring].astype(float)
    a = np.column_stack([x, y, np.ones_like(x)])
    b = x * x + y * y
    (c0, c1, c2), *_ = np.linalg.lstsq(a, b, rcond=None)
    cu, cv = c0 / 2.0, c1 / 2.0
    radius = float(np.sqrt(max(c2 + cu * cu + cv * cv, 0.0)))
    # Контурная линия имеет толщину: подгонка по всей её ширине даёт середину
    # линии — это и есть окружность чертежа.
    if not np.isfinite(radius) or abs(radius - r) > 0.3 * r or np.hypot(cu - u, cv - v) > 0.3 * r:
        return u, v, r
    return float(cu), float(cv), radius
