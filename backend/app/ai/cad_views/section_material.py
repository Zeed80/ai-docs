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


def material_by_outline(gray: Any, line: float) -> Any:
    """Материал разреза как ячейки, ограниченные только ОСНОВНЫМИ линиями.

    `section_material` делит лист на ячейки по всем чернилам: размерные линии
    и стрелки Ø, проходящие через стенку, поперечный канал, надпись режут
    стенку на обрывки, и часть их отсеивается — у полого вала shaft-2 стенка
    рвалась на куски, а расточка по ним выходила Ø14,2 при Ø15 со ступенями
    Ø26–28 у каналов. Здесь стенки ячеек — основные линии без наклонных
    штрихов (штриховка разрывает их — разрывы замыкаются вдоль строк и
    столбцов), ячейка — материал, если штрихи покрывают заметную её долю.
    """
    import cv2
    import numpy as np

    from app.ai.cad_views.extrude_body import main_line_mask

    ink, thick, _ = main_line_mask(np.asarray(gray))
    strokes = hatch_strokes(ink, line)
    # Из стенок вычитаются только ТОНКИЕ наклонные штрихи: фаска под 45° —
    # основная линия, и без неё ячейка крайней ступени вытекала за торец
    # (полый вал shaft-7: разрез обрывался на 706 из 740 px).
    thin_strokes = _thin_hatch_strokes(ink, line)
    thick = thick & (1 - cv2.dilate(thin_strokes, np.ones((3, 3), np.uint8)))
    size = int(0.6 * line) | 1
    thick = cv2.morphologyEx(thick, cv2.MORPH_OPEN, np.ones((size, size), np.uint8))
    reach = int(2.5 * line) | 1
    walls = cv2.morphologyEx(
        thick, cv2.MORPH_CLOSE, np.ones((1, reach), np.uint8)
    ) | cv2.morphologyEx(thick, cv2.MORPH_CLOSE, np.ones((reach, 1), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats((1 - walls).astype(np.uint8), 4)
    border = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])))
    material = np.zeros_like(ink)
    for index in range(1, count):
        if index in border or stats[index][cv2.CC_STAT_AREA] < 9 * line * line:
            continue
        cell = labels == index
        if float(strokes[cell].mean()) > 0.08:
            material[cell] = 1
    return material


def _thin_hatch_strokes(ink: Any, line: float) -> Any:
    """Наклонные штрихи тоньше основной линии (штриховка, не фаска)."""
    import cv2
    import numpy as np

    out = np.zeros_like(ink)
    found = cv2.createLineSegmentDetector(0).detect(((1 - ink) * 255).astype(np.uint8))[0]
    if found is None:
        return out
    distance = cv2.distanceTransform(ink.astype(np.uint8), cv2.DIST_L2, 3)
    height, width = ink.shape[:2]
    for x0, y0, x1, y1 in found.reshape(-1, 4):
        length = math.hypot(x1 - x0, y1 - y0)
        angle = (math.degrees(math.atan2(y1 - y0, x1 - x0)) + 180.0) % 180.0
        if length < 1.5 * line or not (25 <= angle <= 65 or 115 <= angle <= 155):
            continue
        # Толщина — медиана вдоль самого штриха (у края штриховки рядом
        # основная линия контура, окно вокруг середины цепляло её).
        widths = []
        for share in (0.2, 0.35, 0.5, 0.65, 0.8):
            px = int(round(x0 + (x1 - x0) * share))
            py = int(round(y0 + (y1 - y0) * share))
            if 0 <= px < width and 0 <= py < height:
                patch = distance[max(0, py - 1) : py + 2, max(0, px - 1) : px + 2]
                widths.append(2.0 * float(patch.max()))
        if not widths or float(np.median(widths)) > 0.75 * line:
            continue
        cv2.line(out, (int(x0), int(y0)), (int(x1), int(y1)), 1, max(2, int(line)))
    return out
