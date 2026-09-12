"""Проверяльщик центрального отверстия круглой детали — по радиальному профилю.

Центральное отверстие фланца концентрично окружности центров, и поиск в
полосе (как у пластины) их не различает. Зато от центра вида видны все
замкнутые концентрические окружности: для каждого радиуса — доля из 360
направлений, где на окружности есть линия; окружность — полоса радиусов с
долей ≥ 0,8, ширина полосы — толщина линии. Из них исключается контур
(наибольшая), тонкие линии (окружность центров — тонкая, ГОСТ 2.303)
отсекаются толщиной, из оставшихся берётся ближайшая к прочитанному Ø.
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.registry import register
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

_MIN_COVERAGE = 0.8
# Основная линия — не тоньше этой доли толщины контура (тонкая — вдвое тоньше).
_MAIN_SHARE = 0.7


@register("concentric_hole", min_feature_px=8.0)
def verify_concentric_hole(hypothesis: Hypothesis, frame: ViewFrame | None, sheet: Any) -> Verdict:
    """``expected``: ``diameter_mm``; начало системы вида — центр детали."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances

    if frame is None:
        return Verdict(status="unmeasurable", reason="нет системы координат вида")
    diameter = hypothesis.expected.get("diameter_mm")
    if not diameter:
        return Verdict(status="unmeasurable", reason="нет прочитанного Ø")
    gray = np.asarray(sheet)
    circles = concentric_circles(_ink(gray), *frame.origin_px)
    if len(circles) < 2:
        return Verdict(status="unmeasurable", reason="концентрических окружностей нет")
    outer_thickness = circles[-1][1]
    # Толщина в пикселях квантуется: при 100 dpi контур — 3 px, расточка —
    # 2 px, и доля 0,7 отсекала саму расточку (7 фланцев из 24 «не измеримо»).
    # Пиксель допуска от толщины контура держит порог в мере листа.
    main_threshold = min(_MAIN_SHARE * outer_thickness, outer_thickness - 1.0)
    inner = [
        (radius, thickness) for radius, thickness in circles[:-1] if thickness >= main_threshold
    ]
    if not inner:
        return Verdict(status="unmeasurable", reason="внутри контура нет основных окружностей")
    scale = frame.scale_mean
    read_radius_px = float(diameter) / 2.0 / scale
    radius, _thickness = min(inner, key=lambda item: abs(item[0] - read_radius_px))
    measured = round(2.0 * radius * scale, 3)
    _position_tol, diameter_tol = plate_hole_tolerances(scale)
    cx, cy = frame.origin_px
    wrong = abs(measured - float(diameter)) > diameter_tol
    return Verdict(
        status="refuted" if wrong else "confirmed",
        measured={"diameter_mm": measured},
        evidence_bbox_px=(cx - radius, cy - radius, cx + radius, cy + radius),
        anchors_px=((cx, cy),),
        reason=(f"Ø {measured:g} мм, прочитано {float(diameter):g}" if wrong else ""),
    )


def concentric_circles(ink: Any, cx: float, cy: float) -> list[tuple[float, float]]:
    """Замкнутые окружности вокруг центра: ``(радиус середины, толщина)`` по возрастанию."""
    import numpy as np

    height, width = ink.shape
    limit = int(min(cx, cy, width - 1 - cx, height - 1 - cy))
    if limit < 10:
        return []
    radii = np.arange(1, limit + 1, dtype=float)
    angles = np.linspace(0.0, 2.0 * np.pi, 360, endpoint=False)[:, None]
    xs = np.rint(cx + radii[None, :] * np.cos(angles)).astype(int)
    ys = np.rint(cy + radii[None, :] * np.sin(angles)).astype(int)
    polar = ink[ys, xs]
    # Покрытие радиуса — с допуском в пиксель по радиусу (округление, овал фото).
    band = polar.copy()
    band[:, 1:] |= polar[:, :-1]
    band[:, :-1] |= polar[:, 1:]
    closed = band.mean(axis=0) >= _MIN_COVERAGE
    circles: list[tuple[float, float]] = []
    start = None
    for index, flag in enumerate([*closed.tolist(), False]):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            # Толщина — без допуска полосы: он добавил по пикселю с краёв.
            thickness = max(1.0, float(index - start) - 2.0)
            circles.append((float(radii[start] + radii[index - 1]) / 2.0, thickness))
            start = None
    # Центральная «окружность» радиусом в пару пикселей — это само перекрестье.
    return [item for item in circles if item[0] >= 4.0]
