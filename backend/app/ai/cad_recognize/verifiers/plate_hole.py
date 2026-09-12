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
# Угловые сектора подгонки и доля их, покрытая линией, чтобы окружность
# считалась замкнутой (четверть дуги скругления — около 0,25).
_SECTORS = 72
_MIN_COVERAGE = 0.6


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
    radius_px = diameter_mm / 2.0 / frame.scale_mean
    cx, _cy = frame.to_px(x_mm, y_mm if y_mm is not None else 0.0)
    x0b, y0b, x1b, y1b = (int(round(v)) for v in frame.bbox_px)
    # Прочитанному Ø верить нельзя (plate-1: Ø11 прочитан как 6,5), опора —
    # только x: полоса и диапазон радиусов берут отверстие до 2,5 раз больше.
    half = int(round(_RADIUS_SPAN[1] * radius_px + _POSITION_MM / frame.scale_mean)) + 2
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
    tolerance_px = max(2.0, _POSITION_MM / frame.scale_mean)
    on_column = [c for c in circles if abs(c[0] + left - cx) <= tolerance_px + 0.35 * radius_px]
    if not on_column:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=(left, top, right, bottom),
            reason="окружности в полосе есть, но не на заявленном x",
        )
    # Ближайшая к прочитанному y ЗАМКНУТАЯ окружность: дуга скругления угла
    # рядом с угловым отверстием тоже находится Hough-ом (plate-13: Ø9 в углу
    # измерился как Ø17), но линией покрыта едва на четверть.
    fitted = None
    for candidate in sorted(on_column, key=lambda c: abs(c[1] - target_y)):
        result = _fit_circle(strip, *candidate)
        if result is not None and result[3] >= _MIN_COVERAGE:
            fitted = result
            break
    if fitted is None:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=(left, top, right, bottom),
            reason="на заявленном x нет замкнутой окружности",
        )
    u, v, r, _coverage = fitted
    # Уточнение, как у поперечного отверстия вала: подгонка по сектору
    # стартует от радиуса Hough и остаётся у него (Ø ±0,3–0,4 мм на корпусе);
    # радиус — профилем, центр — по серединам штриха на лучах.
    from app.ai.cad_recognize.verifiers.cross_hole import _ring
    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    refined = _ring(_ink(np.ascontiguousarray(strip)), u, v, r)
    if refined is not None:
        u, v, r = refined[0], refined[1], refined[2]
    centre_px = (u + left, v + top)
    measured_x, measured_y = frame.to_mm(*centre_px)
    measured = {
        "x_mm": round(measured_x, 3),
        "y_mm": round(measured_y, 3),
        "diameter_mm": round(2.0 * r * frame.scale_mean, 3),
    }
    bbox = (centre_px[0] - r, centre_px[1] - r, centre_px[0] + r, centre_px[1] + r)
    position_tol, diameter_tol = plate_hole_tolerances(frame.scale_mean)
    problems = []
    if abs(measured["x_mm"] - x_mm) > position_tol:
        problems.append(f"x {measured['x_mm']:g} мм, прочитано {x_mm:g}")
    if y_mm is not None and abs(measured["y_mm"] - y_mm) > position_tol:
        problems.append(f"y {measured['y_mm']:g} мм, прочитано {y_mm:g}")
    if abs(measured["diameter_mm"] - diameter_mm) > diameter_tol:
        problems.append(f"Ø {measured['diameter_mm']:g} мм, прочитано {diameter_mm:g}")
    return Verdict(
        status="refuted" if problems else "confirmed",
        measured=measured,
        evidence_bbox_px=bbox,
        anchors_px=(centre_px,),
        reason="; ".join(problems),
    )


def plate_hole_tolerances(mm_per_px: float) -> tuple[float, float]:
    """Допуски положения и Ø в мм — не меньше пары пикселей листа.

    0,3 мм по Ø при 75 dpi — это меньше одного пикселя: отказ был бы не
    проверки, а арифметики. Пиксельный предел держит допуск в мере листа.
    """
    return max(_POSITION_MM, 2.0 * mm_per_px), max(_DIAMETER_MM, 1.5 * mm_per_px)


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


def _fit_circle(
    strip: Any, u: float, v: float, r: float
) -> tuple[float, float, float, float] | None:
    """Окружность по чернилам в кольце — по угловым секторам, устойчиво к стрелкам.

    Подгонка по всем пикселям кольца сдвигала радиус: к окружности отверстия
    примыкают залитые стрелки её же диаметра, через неё идут выносные
    координат (корпус v7: Ø5,5 мерилось от 4,87 до 6,16). В каждом из секторов
    берётся медианное расстояние чернил до центра — стрелки и линии занимают
    лишь несколько секторов; окружность подгоняется по этим точкам (Kåsa),
    сектора, далёкие от неё, отбрасываются, и подгонка повторяется.
    Возвращает ``(u, v, r, покрытие)`` — долю секторов, где линия есть.
    """
    import numpy as np

    ink = np.asarray(strip) < 160
    ys, xs = np.nonzero(ink)
    if xs.size < 12:
        return None
    distance = np.hypot(xs - u, ys - v)
    ring = np.abs(distance - r) <= max(2.0, 0.25 * r)
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
    coverage = len(points) / _SECTORS
    if len(points) < 8:
        return None
    pts = np.asarray(points)
    for _round in range(2):
        cu, cv, radius = _kasa(pts)
        deviation = np.abs(np.hypot(pts[:, 0] - cu, pts[:, 1] - cv) - radius)
        keep = deviation <= max(1.0, 3.0 * float(np.median(deviation)))
        if int(keep.sum()) < 8:
            break
        pts = pts[keep]
    cu, cv, radius = _kasa(pts)
    if not np.isfinite(radius) or abs(radius - r) > 0.35 * r:
        return None
    return float(cu), float(cv), float(radius), coverage


def _kasa(points: Any) -> tuple[float, float, float]:
    import numpy as np

    x, y = points[:, 0], points[:, 1]
    a = np.column_stack([x, y, np.ones_like(x)])
    (c0, c1, c2), *_ = np.linalg.lstsq(a, x * x + y * y, rcond=None)
    cu, cv = c0 / 2.0, c1 / 2.0
    return cu, cv, float(np.sqrt(max(c2 + cu * cu + cv * cv, 0.0)))
