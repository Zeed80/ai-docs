"""Сечения вала на листе: сплошной круг или кольцо (есть ли полость).

Гейт сборки держит «разрез не прочитан», когда на листе есть сечения, а
полость не прочитана: сплошной вал без проверки сечения признать нельзя.
Живой z4-r4: сечения А-А и Б-Б — это сечения через пазы, сплошные круги с
вырезом; вал сплошной, а блокер стоял.

Сечение ищется на листе, вне главного вида: окружность Хафа, радиус которой
отвечает Ø одной из ступеней профиля (сечения рисуют в масштабе вида), с
замкнутым контуром основной линии. Внутри — штриховка по кольцевым полосам
радиуса (без осевых линий через центр): штриховка по всему радиусу — сплошной
круг, пустая середина при заштрихованном крае — кольцо, то есть полость.
"""

from __future__ import annotations

from typing import Any

# Полосы радиуса (доли R), по которым сравнивается плотность штриховки.
_BANDS = ((0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.85))
# Штриховка есть: доля чернил в полосе. Сплошной — все полосы не меньше
# этой доли максимальной; кольцо — середина пустая при заштрихованном крае.
_HATCH_FLOOR = 0.05
_UNIFORM = 0.4
_RING_EMPTY = 0.3
# Контур сечения — основная линия почти по всей окружности (паз её рвёт).
_COVERAGE = 0.75
# Радиус окружности отвечает Ø ступени с этим допуском.
_DIAMETER_SHARE = 0.06


def section_disks(
    gray: Any,
    main_view_bbox: tuple[float, float, float, float],
    mm_per_px: float,
    step_diameters: list[float],
) -> list[dict[str, Any]]:
    """Круги сечений вне главного вида: ``solid`` — сплошной, ``ring`` — кольцо."""
    import cv2
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    diameters = sorted({float(d) for d in step_diameters if d and d > 0})
    if not diameters or mm_per_px <= 0:
        return []
    gray = np.asarray(gray)
    ink = _ink(gray)
    height, width = gray.shape
    # Поиск — на уменьшенном вдвое листе: Хаф по полному листу медленный.
    small = cv2.medianBlur(cv2.resize(gray, (width // 2, height // 2)), 5)
    r_min = int(0.9 * diameters[0] / 2.0 / mm_per_px / 2.0)
    r_max = int(1.1 * diameters[-1] / 2.0 / mm_per_px / 2.0)
    if r_max <= 4:
        return []
    circles = cv2.HoughCircles(
        small,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=max(4, r_min // 2),
        param1=120,
        param2=40,
        minRadius=max(4, r_min),
        maxRadius=r_max,
    )
    if circles is None:
        return []
    wide = cv2.dilate(ink.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)
    angles = np.linspace(0.0, 2.0 * np.pi, 720, endpoint=False)
    cos, sin = np.cos(angles), np.sin(angles)

    def coverage(cx: float, cy: float, r: float) -> float:
        xs = np.clip(np.round(cx + r * cos).astype(int), 0, width - 1)
        ys = np.clip(np.round(cy + r * sin).astype(int), 0, height - 1)
        return float(wide[ys, xs].mean())

    x0, y0, x1, y1 = main_view_bbox
    found: list[dict[str, Any]] = []
    for sx, sy, sr in circles[0]:
        cx, cy, r = 2.0 * float(sx), 2.0 * float(sy), 2.0 * float(sr)
        if x0 - r <= cx <= x1 + r and y0 - r <= cy <= y1 + r:
            continue  # на главном виде — выносные круги деталей, не сечения
        diameter = 2.0 * r * mm_per_px
        step = min(diameters, key=lambda d: abs(d - diameter))
        if abs(step - diameter) > _DIAMETER_SHARE * step:
            continue
        if any(
            abs(cx - f["center_px"][0]) < 0.5 * r and abs(cy - f["center_px"][1]) < 0.5 * r
            for f in found
        ):
            continue
        best = (coverage(cx, cy, r), cx, cy, r)
        for dx in (-4.0, 0.0, 4.0):
            for dy in (-4.0, 0.0, 4.0):
                for dr in (-4.0, 0.0, 4.0):
                    value = coverage(cx + dx, cy + dy, r + dr)
                    if value > best[0]:
                        best = (value, cx + dx, cy + dy, r + dr)
        cover, cx, cy, r = best
        if cover < _COVERAGE:
            continue
        left, right = int(max(0, cx - r)), int(min(width, cx + r))
        top, bottom = int(max(0, cy - r)), int(min(height, cy + r))
        yy, xx = np.mgrid[top:bottom, left:right]
        dx_, dy_ = xx - cx, yy - cy
        radius = np.hypot(dx_, dy_) / r
        # Осевые линии через центр — не штриховка.
        off_axes = (np.abs(dx_) > 0.04 * r) & (np.abs(dy_) > 0.04 * r)
        patch = ink[top:bottom, left:right]
        bands = []
        for low, high in _BANDS:
            mask = (radius >= low) & (radius < high) & off_axes
            bands.append(float(patch[mask].mean()) if mask.any() else 0.0)
        top_band = max(bands)
        solid = min(bands) >= _HATCH_FLOOR and min(bands) >= _UNIFORM * top_band
        outer = max(bands[2:])
        ring = outer >= _HATCH_FLOOR and bands[0] < _RING_EMPTY * outer
        found.append(
            {
                "center_px": [round(cx, 1), round(cy, 1)],
                "diameter_mm": round(diameter, 2),
                "step_diameter_mm": step,
                "coverage": round(cover, 2),
                "bands": [round(v, 3) for v in bands],
                "solid": bool(solid),
                "ring": bool(ring),
            }
        )
    return found
