"""Проверяльщик поперечного отверстия вала: положение по оси и Ø по главному виду.

Главный вид вала с пазом — вид `bottom` (Ф3.0a): сквозное поперечное
отверстие под 0° на нём — окружность с центром на оси (Ф3.0b проставляет
её Ø и положение центра). Прочитанная станция выбирает окружность на оси, её
центр и Ø меряются — тем же приёмом, что у отверстия пластины (`plate_hole`):
Hough в полосе и подгонка по секторам кольца, устойчивая к стрелкам и
выносным. На оси же стоят подписи Ø ступеней — «0» и «Ø» в них тоже
окружности; от них спасает только замкнутость и центр строго на оси.
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.registry import register
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Во сколько раз настоящее отверстие может быть больше прочитанного (Hough
# в `plate_hole` берёт 0,4…2,5), и запас поиска вдоль оси, мм.
_RADIUS_SPAN_MAX = 2.5
_REACH_MM = 6.0
_MIN_COVERAGE = 0.6
# Штриховка за окружностью (`keyway._hatch_score`): у отверстия порог выше,
# чем у паза, — выноска Ø к отверстию сама идёт под 45° и даёт до 0,053
# (shaft-12, shaft-17, shaft-28), настоящая штриховка разреза — от 0,15.
_HATCH_FLOOR_HOLE = 0.1
# Кольцо, а не пятно: радиус не меньше полутора толщин линии (стрелка выноски
# у Ø3 — «окружность» r 5,5 px при толщине 4, shaft-4).
_MIN_RADIUS_TO_STROKE = 1.5
# Во сколько раз найденный радиус может отличаться от прочитанного: дуга
# скругления паза рядом — «Ø6,4» при прочитанном Ø3 (shaft-10).
_RADIUS_RATIO = (0.55, 1.8)


@register("cross_hole", min_feature_px=4.0)
def verify_cross_hole(hypothesis: Hypothesis, frame: ViewFrame | None, sheet: Any) -> Verdict:
    """``expected``: ``axial_position_mm``, ``diameter_mm``; ``sheet`` — серый лист."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.keyway import _hatch_score
    from app.ai.cad_recognize.verifiers.plate_frame import _ink
    from app.ai.cad_recognize.verifiers.plate_hole import (
        _fit_circle,
        _hough,
        plate_hole_tolerances,
    )
    from app.ai.cad_recognize.verifiers.shaft_profile import _MIN_LINE_PX

    if frame is None:
        return Verdict(status="unmeasurable", reason="главный вид вала на листе не найден")
    station = hypothesis.expected.get("axial_position_mm")
    diameter = hypothesis.expected.get("diameter_mm")
    if not isinstance(station, (int, float)) or not isinstance(diameter, (int, float)):
        return Verdict(status="unmeasurable", reason="нет прочитанного положения или Ø")
    gray = np.asarray(sheet)
    scale = frame.scale_mean
    x0, axis = frame.origin_px
    radius_px = float(diameter) / 2.0 / scale
    cx = x0 + float(station) / frame.mm_per_px
    reach = _RADIUS_SPAN_MAX * radius_px + _REACH_MM / frame.mm_per_px
    left, right = max(0, int(cx - reach)), min(gray.shape[1], int(cx + reach) + 1)
    rise = _RADIUS_SPAN_MAX * radius_px + 4
    top, bottom = max(0, int(axis - rise)), min(gray.shape[0], int(axis + rise) + 1)
    if right - left < 2 * radius_px or bottom - top < 2 * radius_px:
        return Verdict(status="unmeasurable", reason="область отверстия вне вида")
    strip = np.ascontiguousarray(gray[top:bottom, left:right])
    axis_local = axis - top
    on_axis = [
        circle
        for circle in _hough(strip, radius_px)
        if abs(circle[1] - axis_local) <= max(2.0, 0.35 * circle[2])
    ]
    # Ближайшая к прочитанной станции ЗАМКНУТАЯ окружность с центром на оси.
    # Замкнутость — по радиальному профилю (`_ring`), а не по доле секторов
    # подгонки: у Ø3 стрелка выноски больше самого отверстия, и подгонку
    # проходили стрелка с линией (shaft-4, shaft-10: «Ø4,9», «Ø5,8»).
    ink = _ink(strip)
    fitted = None
    hatched = False
    for candidate in sorted(on_axis, key=lambda c: abs(c[0] + left - cx)):
        result = _fit_circle(strip, *candidate)
        if result is None or result[3] < _MIN_COVERAGE:
            continue
        refined = _ring(ink, *result[:3])
        if refined is None:
            continue
        ru, rv, rr, thickness = refined
        if abs(rv - axis_local) > max(2.0, 0.15 * rr):
            continue
        if rr < _MIN_RADIUS_TO_STROKE * thickness:
            continue
        if not _RADIUS_RATIO[0] * radius_px <= rr <= _RADIUS_RATIO[1] * radius_px:
            continue
        # Разрез полого вала: окружность в штриховке — не отверстие лицом
        # (shaft-8, shaft-19: Ø12,3 и Ø11,2 при прочитанных 6 и 5); отверстие
        # ищется на следующем виде того же вала.
        if _hatch_score(ink, rv, rr, ru, ru) >= _HATCH_FLOOR_HOLE:
            hatched = True
            continue
        fitted = (ru, rv, rr, thickness)
        break
    if fitted is None:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=(left, top, right, bottom),
            reason=(
                "окружность у прочитанного положения — в штриховке разреза"
                if hatched
                else "замкнутой окружности на оси у прочитанного положения нет"
            ),
        )
    u, v, r, thickness = fitted
    # Грубый лист — как у ступеней и пазов: при линии тоньше 4,5 px (ниже
    # 250 dpi) стрелки выносок сливаются с окружностью, и замер на 200 dpi
    # давал четверть неверных вердиктов.
    if thickness < _MIN_LINE_PX:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=(left, top, right, bottom),
            reason=(
                f"лист слишком грубый: линия окружности {thickness:.1f} px "
                f"(нужно от {_MIN_LINE_PX:g})"
            ),
        )
    centre = (u + left, v + top)
    measured = {
        "axial_position_mm": round((centre[0] - x0) * frame.mm_per_px, 3),
        "diameter_mm": round(2.0 * r * scale, 3),
    }
    position_tol, diameter_tol = plate_hole_tolerances(scale)
    problems = []
    if abs(measured["axial_position_mm"] - float(station)) > position_tol:
        problems.append(
            f"положение {measured['axial_position_mm']:g} мм, прочитано {float(station):g}"
        )
    if abs(measured["diameter_mm"] - float(diameter)) > diameter_tol:
        problems.append(f"Ø {measured['diameter_mm']:g} мм, прочитано {float(diameter):g}")
    return Verdict(
        status="refuted" if problems else "confirmed",
        measured=measured,
        evidence_bbox_px=(centre[0] - r, centre[1] - r, centre[0] + r, centre[1] + r),
        anchors_px=(centre,),
        reason="; ".join(problems),
    )


def _ring(ink: Any, u: float, v: float, r: float) -> tuple[float, float, float, float] | None:
    """Центр, радиус и толщина окружности: середина штриха по лучам, радиус — профилем.

    Подгонка по сектору кольца стартует от радиуса Hough (мимо на 20 %), и в
    кольцо попадает часть штриха; стрелка выноски Ø касается окружности и
    тянет центр к себе (корпус v7: Ø ±0,4 мм, положение +0,1…0,3). Радиус —
    середина полосы радиусов, где линия идёт почти по всем углам
    (`concentric_circles`: выносные и стрелки такой полосы не дают). Центр —
    Kåsa по серединам штриха на лучах; луч, где штрих слит со стрелкой или
    линией (заметно толще окружности), отбрасывается.
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers.concentric_hole import concentric_circles
    from app.ai.cad_recognize.verifiers.plate_hole import _kasa

    height, width = ink.shape

    def nearest_ring(cu: float, cv: float) -> tuple[float, float] | None:
        rings = concentric_circles(ink, cu, cv)
        if not rings:
            return None
        radius, thickness = min(rings, key=lambda ring: abs(ring[0] - r))
        return (radius, thickness) if abs(radius - r) <= 0.35 * r else None

    ring = nearest_ring(u, v)
    if ring is None:
        return None
    radius, thickness = ring
    for _round in range(2):
        steps = np.arange(max(1.0, radius - 2.0 * thickness), radius + 2.0 * thickness + 0.5, 0.5)
        points = []
        for angle in np.linspace(0.0, 2.0 * np.pi, 72, endpoint=False):
            xs = np.rint(u + steps * np.cos(angle)).astype(int)
            ys = np.rint(v + steps * np.sin(angle)).astype(int)
            inside = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
            hit = np.zeros(steps.size, dtype=bool)
            hit[inside] = ink[ys[inside], xs[inside]]
            index = np.nonzero(hit)[0]
            if index.size == 0:
                continue
            runs = np.split(index, np.nonzero(np.diff(index) > 1)[0] + 1)
            run = min(runs, key=lambda item: abs((steps[item[0]] + steps[item[-1]]) / 2 - radius))
            if (steps[run[-1]] - steps[run[0]]) > 1.6 * thickness + 1.0:
                continue
            middle = (steps[run[0]] + steps[run[-1]]) / 2.0
            points.append((u + middle * np.cos(angle), v + middle * np.sin(angle)))
        if len(points) < 24:
            break
        pts = np.asarray(points)
        cu, cv, fitted = _kasa(pts)
        deviation = np.abs(np.hypot(pts[:, 0] - cu, pts[:, 1] - cv) - fitted)
        keep = deviation <= max(0.75, 3.0 * float(np.median(deviation)))
        if int(keep.sum()) >= 24:
            cu, cv, fitted = _kasa(pts[keep])
        ring = nearest_ring(cu, cv)
        if ring is None:
            break
        u, v = float(cu), float(cv)
        radius, thickness = ring
    return u, v, radius, thickness
