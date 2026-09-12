"""Проверяльщик окружности болтов: число, PCD, Ø отверстия и фаза — по плану.

Гипотеза ридера — «N отв. Øa на окружности Øb». Фазу массива лист не несёт
(углового размера перечерчивание не ставит), и ридер пишет 0°: проверяльщик
её меряет — это новое знание, а не только проверка.

Поиск — малые окружности в кольце вокруг прочитанной окружности центров
(от половины до полутора её радиусов): каждая уточняется по секторам и
принимается, только если замкнута. Число, PCD (медиана расстояний до
центра), Ø (медиана) и фаза (угол ближайшего к +u отверстия, по модулю шага)
сравниваются с прочитанным. Система координат — с началом в центре детали
(`circle_frame.locate_circle_frame`).
"""

from __future__ import annotations

import math
from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.plate_hole import (
    _MIN_COVERAGE,
    _fit_circle,
    _hough,
    plate_hole_tolerances,
)
from app.ai.cad_recognize.verifiers.registry import register
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Допуск фазы, градусы: на листе нет углового размера, а центр отверстия
# меряется с точностью долей миллиметра — на PCD 70 это меньше градуса.
_PHASE_DEG = 2.0


@register("bolt_circle", min_feature_px=8.0)
def verify_bolt_circle(hypothesis: Hypothesis, frame: ViewFrame | None, sheet: Any) -> Verdict:
    """``expected``: ``count``, ``pcd_mm``, ``hole_diameter_mm``, ``start_angle_deg``."""
    import numpy as np

    if frame is None:
        return Verdict(status="unmeasurable", reason="нет системы координат вида")
    expected = hypothesis.expected
    pcd, hole = expected.get("pcd_mm"), expected.get("hole_diameter_mm")
    if not pcd or not hole:
        return Verdict(status="unmeasurable", reason="нет прочитанных PCD или Ø отверстия")
    gray = np.asarray(sheet)
    scale = frame.scale_mean
    cx, cy = frame.origin_px
    ring_outer = 1.5 * pcd / 2.0 / scale
    ring_inner = 0.5 * pcd / 2.0 / scale
    x0, y0 = max(0, int(cx - ring_outer)), max(0, int(cy - ring_outer))
    x1 = min(gray.shape[1], int(cx + ring_outer) + 1)
    y1 = min(gray.shape[0], int(cy + ring_outer) + 1)
    roi = gray[y0:y1, x0:x1]
    radius_px = hole / 2.0 / scale
    # Уточнение каждого отверстия — как у пластины (`cross_hole._ring`):
    # подгонка по сектору оставалась у радиуса Hough, Ø мелких отверстий
    # массива выходил на +0,4 мм.
    from app.ai.cad_recognize.verifiers.cross_hole import _ring
    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    roi_ink = _ink(np.ascontiguousarray(roi))
    holes: list[tuple[float, float, float]] = []
    for u, v, r in _hough(roi, radius_px):
        distance = math.hypot(u + x0 - cx, v + y0 - cy)
        if not ring_inner <= distance <= ring_outer:
            continue
        fitted = _fit_circle(roi, u, v, r)
        if fitted is None or fitted[3] < _MIN_COVERAGE:
            continue
        refined = _ring(roi_ink, fitted[0], fitted[1], fitted[2])
        if refined is not None:
            fitted = (refined[0], refined[1], refined[2], fitted[3])
        fu, fv, fr = fitted[0] + x0, fitted[1] + y0, fitted[2]
        if all(math.hypot(fu - hx, fv - hy) > fr for hx, hy, _ in holes):
            holes.append((fu, fv, fr))
    # В кольцо попадают и чужие замкнутые окружности — петли цифр подписей
    # («0», «6», «8»), центральное отверстие соседнего вида. Первый прогон
    # корпуса v7 насчитал 6, 15, 26 «отверстий» вместо 4, 6, 3. Отверстия
    # массива — это группа окружностей на ОДНОМ расстоянии от центра и
    # одного размера: берётся наибольшая такая группа.
    holes = _dominant_ring(holes, (cx, cy), pcd / 2.0 / scale)
    # На том же кольце и того же размера бывает и петля подписи — «Ø73»
    # ставится прямо на окружности центров. Отверстия массива отличает шаг:
    # берётся наибольший правильный многоугольник, все вершины которого есть.
    holes = _regular_subset(holes, (cx, cy))
    if len(holes) < 2:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=(x0, y0, x1, y1),
            reason=f"на окружности центров найдено отверстий: {len(holes)}",
        )
    radii = [math.hypot(hx - cx, hy - cy) for hx, hy, _ in holes]
    count = len(holes)
    step = 360.0 / count
    # Угол — против часовой от +u, v вверх (у листа y вниз, отсюда минус).
    angles = sorted(math.degrees(math.atan2(-(hy - cy), hx - cx)) % step for hx, hy, _ in holes)
    phase = _circular_mean(angles, step)
    measured = {
        "count": count,
        "pcd_mm": round(2.0 * float(np.median(radii)) * scale, 3),
        "hole_diameter_mm": round(2.0 * float(np.median([r for *_, r in holes])) * scale, 3),
        "start_angle_deg": round(phase, 2),
    }
    position_tol, diameter_tol = plate_hole_tolerances(scale)
    problems = []
    if expected.get("count") and int(expected["count"]) != count:
        problems.append(f"отверстий {count}, прочитано {expected['count']}")
    if abs(measured["pcd_mm"] - float(pcd)) > 2.0 * position_tol:
        problems.append(f"PCD {measured['pcd_mm']:g} мм, прочитано {pcd:g}")
    if abs(measured["hole_diameter_mm"] - float(hole)) > diameter_tol:
        problems.append(f"Ø {measured['hole_diameter_mm']:g} мм, прочитано {hole:g}")
    read_phase = expected.get("start_angle_deg")
    if read_phase is not None and _angle_gap(float(read_phase) % step, phase, step) > _PHASE_DEG:
        problems.append(f"фаза {phase:.1f}°, прочитано {float(read_phase):g}°")
    return Verdict(
        status="refuted" if problems else "confirmed",
        measured=measured,
        evidence_bbox_px=(x0, y0, x1, y1),
        anchors_px=tuple((hx, hy) for hx, hy, _ in holes),
        reason="; ".join(problems),
    )


def _dominant_ring(
    holes: list[tuple[float, float, float]], centre: tuple[float, float], read_radius_px: float
) -> list[tuple[float, float, float]]:
    """Наибольшая группа окружностей с общим радиусом кольца и общим размером.

    При равенстве групп — ближайшая к прочитанной окружности центров: ей
    верить нельзя как числу (PCD проверяется), но как подсказке — можно.
    """
    cx, cy = centre
    best: tuple[tuple[int, float], list[tuple[float, float, float]]] | None = None
    for hx, hy, hr in holes:
        ring = math.hypot(hx - cx, hy - cy)
        members = [
            item
            for item in holes
            if abs(math.hypot(item[0] - cx, item[1] - cy) - ring) <= max(2.0, 0.03 * ring)
            and abs(item[2] - hr) <= max(1.5, 0.2 * hr)
        ]
        key = (len(members), -abs(ring - read_radius_px))
        if best is None or key > best[0]:
            best = (key, members)
    return best[1] if best else []


def _regular_subset(
    holes: list[tuple[float, float, float]], centre: tuple[float, float]
) -> list[tuple[float, float, float]]:
    """Наибольшее подмножество, лежащее в вершинах правильного k-угольника.

    Допуск по углу — пара пикселей дуги, но не меньше 2°. Если правильного
    многоугольника нет вовсе (две точки — всегда многоугольник), группа
    остаётся как есть.
    """
    if len(holes) < 3:
        return holes
    cx, cy = centre
    angles = [math.degrees(math.atan2(-(hy - cy), hx - cx)) % 360.0 for hx, hy, _ in holes]
    ring = sum(math.hypot(hx - cx, hy - cy) for hx, hy, _ in holes) / len(holes)
    tolerance = max(2.0, math.degrees(2.0 / max(ring, 1.0)))
    for k in range(len(holes), 2, -1):
        for start in angles:
            picked = []
            for j in range(k):
                target = (start + 360.0 * j / k) % 360.0
                nearest = min(range(len(holes)), key=lambda t: _angle_gap(angles[t], target, 360.0))
                if _angle_gap(angles[nearest], target, 360.0) > tolerance:
                    break
                picked.append(nearest)
            if len(set(picked)) == k:
                return [holes[t] for t in picked]
    return holes


def _circular_mean(angles: list[float], period: float) -> float:
    """Средний угол по модулю ``period`` (0 и почти ``period`` — рядом)."""
    s = sum(math.sin(2 * math.pi * a / period) for a in angles)
    c = sum(math.cos(2 * math.pi * a / period) for a in angles)
    return (math.degrees(math.atan2(s, c)) * period / 360.0) % period


def _angle_gap(a: float, b: float, period: float) -> float:
    gap = abs(a - b) % period
    return min(gap, period - gap)
