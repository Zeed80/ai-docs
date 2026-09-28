"""Этап B2: материал разреза — где деталь, а где пусто, как видит инженер.

Не по толщине линий (на скане она ненадёжна), а по смыслу штриховки:
лист делится линиями на ячейки фона; ячейка — материал, если её граница
в заметной доле из штрихов под 45°/135°. Штрихи штриховки часто не доходят
до кромок — ячейка стенки одна, со штрихами внутри; это тоже материал.
Незаштрихованное — не материал, даже тонкая полоса между линиями: в
разрезе это поверхность за секущей плоскостью («Опора пружин»: полоса между
дугами — стенки отверстия Ø5, а не шейка). Разрез тела вращения
симметричен оси: материал без зеркальной пары (треугольник у выноски) —
не материал.
"""

from __future__ import annotations

import math
from typing import Any


def ink_mask(gray: Any, line: float) -> Any:
    import cv2
    import numpy as np

    g = np.asarray(gray)
    background = cv2.medianBlur(g, 31).astype(float)
    ink = ((background - g) > 25).astype(np.uint8)
    kernel = max(3, int(line))
    return cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((kernel, kernel), np.uint8))


def hatch_strokes(ink: Any, line: float) -> Any:
    """Штрихи под 45°/135° (детектор отрезков, допуск угла ±20°)."""
    import cv2
    import numpy as np

    out = np.zeros_like(ink)
    found = cv2.createLineSegmentDetector(0).detect(((1 - ink) * 255).astype(np.uint8))[0]
    if found is None:
        return out
    for x0, y0, x1, y1 in found.reshape(-1, 4):
        length = math.hypot(x1 - x0, y1 - y0)
        angle = (math.degrees(math.atan2(y1 - y0, x1 - x0)) + 180.0) % 180.0
        if length >= 1.5 * line and (25 <= angle <= 65 or 115 <= angle <= 155):
            cv2.line(out, (int(x0), int(y0)), (int(x1), int(y1)), 1, max(2, int(line)))
    return out


def symmetry_axis(mask: Any) -> int:
    """Строка наибольшей зеркальной симметрии маски."""
    height = mask.shape[0]
    best = (-1.0, height // 2)
    for y in range(height // 5, 4 * height // 5):
        half = min(y, height - y)
        above = mask[y - half : y].astype(float)
        below = mask[y : y + half][::-1].astype(float)
        score = float((above * below).sum()) / (float(above.sum() + below.sum()) + 1.0)
        if score > best[0]:
            best = (score, y)
    return best[1]


def section_material(
    gray: Any,
    line: float,
    *,
    revolve: bool = True,
    axis: int | None = None,
    main_boundary: float = 0.7,
) -> tuple[Any, int]:
    """Маска материала разреза и (для тела вращения) строка оси."""
    import cv2
    import numpy as np

    g = np.asarray(gray)
    ink = ink_mask(g, line)
    strokes = cv2.dilate(hatch_strokes(ink, line), np.ones((3, 3), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats((1 - ink).astype(np.uint8), 4)
    border = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])))
    size = g.size
    material = np.zeros_like(ink)
    for index in range(1, count):
        if index in border:
            continue
        x, y, w, h, area = stats[index]
        if area < 4:
            continue
        pad = 4
        window = (slice(max(0, y - pad), y + h + pad), slice(max(0, x - pad), x + w + pad))
        face = (labels[window] == index).astype(np.uint8)
        ring = cv2.dilate(face, np.ones((5, 5), np.uint8)) & (1 - face)
        share = float((ring & strokes[window]).sum()) / max(1.0, float(ring.sum()))
        # Крупная ячейка — обычно фон вида; но при светлой тонкой штриховке,
        # не попавшей в чернила, вся заштрихованная полоса — одна ячейка
        # (втулка p015: 54 тыс. px, штрихи на 73 % границы).
        if share > (0.5 if area >= 0.05 * size else 0.15):
            material[labels == index] = 1
    if axis is None:
        axis = symmetry_axis(material)
    # Половина вида + половина разреза (ЕСКД): штриховка только с одной
    # стороны оси — зеркальной пары у неё нет по устройству чертежа.
    rows = np.nonzero(material)[0]
    one_sided = rows.size > 0 and ((rows < axis).mean() > 0.9 or (rows > axis).mean() > 0.9)
    if revolve and not one_sided:
        mirrored = np.zeros_like(material)
        height = material.shape[0]
        for y in range(height):
            twin = 2 * axis - y
            if 0 <= twin < height:
                mirrored[y] = material[twin]
        mirrored = cv2.dilate(mirrored, np.ones((int(3 * line), int(3 * line)), np.uint8))
        parts_n, parts = cv2.connectedComponents(material, 8)
        for part in range(1, parts_n):
            member = parts == part
            if (mirrored[member] > 0).mean() < 0.5:
                material[member] = 0
    # Штрихи и линии внутри стенки — тоже материал: без этого столбец рвётся
    # на каждом штрихе, и «расточкой» становится первый треугольник штриховки.
    near = cv2.dilate(material, np.ones((9, 9), np.uint8))
    material = material | (strokes & near)
    closing = max(3, int(line))
    material = cv2.morphologyEx(material, cv2.MORPH_CLOSE, np.ones((closing, closing), np.uint8))
    # Заштрихованная область разреза ограничена основными линиями контура;
    # блок надписи между размерными линиями (курсивные цифры «Ø7,6», «18°»
    # дают наклонные «штрихи») — тонкими. Область, чья граница лежит на
    # основных линиях меньше чем на 70 %, — не материал («Опора»: стенки
    # 0,87–1,0, блок «Ø7,6» — 0,53).
    from app.ai.cad_views.extrude_body import main_line_mask

    _ink, thick, _line = main_line_mask(g)
    regions_n, regions, _st, _c = cv2.connectedComponentsWithStats(
        cv2.dilate(material, np.ones((3, 3), np.uint8)), 8
    )
    grow = np.ones((int(line) | 1, int(line) | 1), np.uint8)
    for index in range(1, regions_n):
        region = (regions == index).astype(np.uint8)
        ring = cv2.dilate(region, grow) & (1 - region)
        on_main = float((ring & thick).sum()) / max(1.0, float(ring.sum()))
        if on_main < main_boundary:
            material[region > 0] = 0
    return material, axis
