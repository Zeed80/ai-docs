"""Призматическая деталь по видам листа: пересечение выдавленных контуров.

Инженер восстанавливает корпус, кронштейн, уголок так: каждый вид — контур
детали, выдавленный насквозь вдоль взгляда этого вида; деталь — их
пересечение. Раскладка видов — ЕСКД (первый угол) относительно главного:
вид под главным — «сверху» по отношению к нему, справа — «слева», над —
«снизу», слева — «справа». Отсюда без угадывания знаков следует, какая ось
детали идёт по горизонтали и вертикали каждого вида.

Система детали — рамка главного вида: X — вправо, Z — вверх, Y — вглубь
листа. Вид под главным: вправо +X, вверх +Y. Вид справа от главного:
вправо −Y, вверх +Z. Вид над главным: вправо +X, вверх −Y. Вид слева от
главного: вправо +Y, вверх +Z. Начало — левый нижний ближний угол габарита.
"""

from __future__ import annotations

from typing import Any

# Штриховка корпуса ограничена основными линиями не так плотно, как у тела
# вращения: по стенкам идут штриховые невидимого контура и выноски
# (корпус G2, разрез A-A: 0,65 при пороге 0,7 — штриховка не находилась).
_MAIN_BOUNDARY = 0.55


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def fit_edges_scale(
    outlines_: list[Any],
    linear: list[float],
    diameters: list[float],
    body_extents: list[float] | None = None,
) -> tuple[float, list[str]] | None:
    """мм/px по расстояниям между кромками контуров видов и Ø отверстий.

    Габарит контура плана включает приливы («80» надписан по телу, а контур
    шире на прилив), поэтому пара «ширина/высота контура» подбиралась
    случайная. Кандидаты — надпись / расстояние между вертикальными или
    горизонтальными кромками любого вида; побеждает масштаб, при котором
    больше разных надписей совпадает, при равенстве — более крупные.

    ``body_extents`` — габариты тела на видах (px): габарит на чертеже
    проставлен всегда, и масштаб, при котором он надписан, важнее числа
    случайных совпадений (корпус: 0,41 мм/px «объяснял» 8 надписей
    расстояниями между кромками приливов при верном 0,17)."""
    import itertools

    xs_sets: list[list[float]] = []
    for outline in outlines_:
        pts = outline.points + outline.points[:1]
        vertical = set()
        horizontal = set()
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            if abs(ax - bx) <= 0.035 * abs(ay - by) and abs(ay - by) > 3 * outline.line:
                vertical.add(round((ax + bx) / 2.0, 1))
            if abs(ay - by) <= 0.035 * abs(ax - bx) and abs(ax - bx) > 3 * outline.line:
                horizontal.add(round((ay + by) / 2.0, 1))
        for coords in (sorted(vertical), sorted(horizontal)):
            if len(coords) >= 2:
                xs_sets.append(coords)
    distances = sorted(
        {
            round(b - a, 1)
            for coords in xs_sets
            for a, b in itertools.combinations(coords, 2)
            if b - a > 5
        },
        reverse=True,
    )[:80]
    holes = [2 * r for outline in outlines_ for _cx, _cy, r in outline.holes]
    labels = sorted({v for v in linear if v > 0}, reverse=True)
    if not labels or not distances:
        return None
    extents = [e for e in (body_extents or []) if e > 5]
    best: tuple[bool, int, int, float, float, list[str]] | None = None
    for label in labels[:25]:
        for distance in [*distances, *extents]:
            scale = label / distance
            hits: set[str] = set()
            weight = 0.0
            for other in labels:
                if min(abs(d * scale - other) for d in distances) <= 0.02 * other:
                    hits.add(f"{other:g}")
                    weight += other
            for d in holes:
                near = min(diameters, key=lambda v: abs(v - d * scale), default=None)
                if near is not None and abs(near - d * scale) <= 0.04 * near:
                    hits.add(f"Ø{near:g}")
            body = sum(
                1
                for e in extents
                if any(abs(e * scale - other) <= 0.02 * other for other in labels)
            )
            # Наибольшая надпись — габарит, он обязан лечь на кромки. Иначе
            # вдвое меньший масштаб объясняет столько же надписей их
            # половинками (корпус 10: 150/75, 80/40, 40/20 — все на листе).
            largest = any(
                abs(d * scale - labels[0]) <= 0.02 * labels[0] for d in [*distances, *extents]
            )
            key = (largest, body, len(hits), weight)
            if best is None or key > best[:4]:
                best = (largest, body, len(hits), weight, scale, sorted(hits))
    if best is None:
        return None
    return best[4], best[5]


def _crop(gray: Any, outline: Any, pad: int) -> tuple[Any, int, int]:
    x, y, w, h = outline.box
    x0, y0 = max(0, x - pad), max(0, y - pad)
    return gray[y0 : y + h + pad, x0 : x + w + pad], x0, y0


def _inside(outline: Any, px: float, py: float) -> bool:
    fx, fy = outline.filled_origin
    lx, ly = int(px - fx), int(py - fy)
    filled = outline.filled
    return 0 <= ly < filled.shape[0] and 0 <= lx < filled.shape[1] and bool(filled[ly, lx])


class _Void(list):
    """Вершины пустоты разреза (px листа) и стороны, найденные по стенкам."""

    walled: tuple[bool, bool, bool, bool] = (False, False, False, False)


def section_voids(
    gray: Any,
    outline: Any,
    rect: tuple[float, float, float, float] | None = None,
    thick_sheet: Any = None,
) -> list[list[tuple[float, float]]]:
    """Сечения полостей на разрезе: незаштрихованные области внутри контура.

    Вид считается разрезом, если штриховка занимает заметную часть контура;
    на простом виде пустое место внутри контура — не полость, а грань."""
    import cv2
    import numpy as np

    from app.ai.cad_views.section_material import section_material

    line = outline.line
    pad = int(2 * line)
    crop, x0, y0 = _crop(gray, outline, pad)
    material, _axis = section_material(crop, line, revolve=False, main_boundary=_MAIN_BOUNDARY)
    fx, fy = outline.filled_origin
    region = np.zeros_like(material)
    fh, fw = outline.filled.shape
    oy, ox = fy - y0, fx - x0
    ys = slice(max(0, oy), min(region.shape[0], oy + fh))
    xs = slice(max(0, ox), min(region.shape[1], ox + fw))
    region[ys, xs] = outline.filled[
        ys.start - oy : ys.stop - oy, xs.start - ox : xs.stop - ox
    ].astype(region.dtype)
    if rect is not None:
        # Только тело вида: контур захватывает размеры под видом, и кольцо
        # пустоты у кромки тела выходило «без материала».
        left, top, right, bottom = (int(round(v)) for v in rect)
        body = np.zeros_like(region)
        body[
            max(0, top - y0) : max(0, bottom - y0 + 1), max(0, left - x0) : max(0, right - x0 + 1)
        ] = 1
        region = region & body
    if region.sum() == 0 or float((material & region).sum()) < 0.15 * float(region.sum()):
        return []
    # Полость, открытая на грань, лежит вне залитого контура (кромки поперёк
    # проёма на разрезе нет) — ячейки ищутся во всём теле вида.
    space = body if rect is not None else region
    from app.ai.cad_views.extrude_body import main_line_mask

    grow = np.ones((int(line) | 1, int(line) | 1), np.uint8)
    if thick_sheet is not None:
        # Граница основная/тонкая — по всему листу: по вырезу вида размерная
        # «50» у стенки шла основной и отрезала от полости полосу 2,5 мм.
        thick = thick_sheet[y0 : y0 + crop.shape[0], x0 : x0 + crop.shape[1]]
    else:
        _ink, thick, _line = main_line_mask(crop)
    # Полость на разрезе — место без штриховки, отрезанное основными линиями
    # (её контур и кромка тела). У размеров в стенке штриховка расчищена, и
    # пустота уходит в стенку коридорами — их срезает размыкание ниже;
    # сплошная штриховка (смыкание пропусков под цифрами) в пустоту не входит.
    closed = cv2.morphologyEx(thick, cv2.MORPH_CLOSE, np.ones((int(2 * line) | 1,) * 2, np.uint8))
    size = int(6 * line) | 1
    solid = cv2.morphologyEx(material, cv2.MORPH_CLOSE, np.ones((size, size), np.uint8))
    cells = space & (1 - closed) & (1 - cv2.dilate(solid, grow))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(cells.astype(np.uint8), 4)
    wide = np.ones((int(8 * line) | 1, int(8 * line) | 1), np.uint8)  # на 4 линии наружу
    lines = cv2.dilate(thick, grow)
    strokes = None  # штрихи штриховки — считаются, только если понадобятся
    polygons = []
    for index in range(1, count):
        if stats[index][cv2.CC_STAT_AREA] < (6 * line) ** 2:
            continue
        mask = (labels == index).astype(np.uint8)
        # Цифры и стрелки размеров внутри полости — дыры в пустоте.
        outer, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        mask = cv2.drawContours(np.zeros_like(mask), outer, -1, 1, -1)
        # Ячейка утекает в стенку узким коридором (разрыв контура у стрелки,
        # расчистка штриховки у размера): размыкание шириной в 0,15
        # полости срезает коридоры и оставляет саму полость.
        _x, _y, cw, ch = (int(v) for v in stats[index][:4])
        core = int(max(6 * line, 0.15 * min(cw, ch))) | 1
        opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((core, core), np.uint8))
        parts, part_labels, part_stats, _ = cv2.connectedComponentsWithStats(opened, 4)
        if parts < 2:
            continue
        biggest = 1 + int(np.argmax(part_stats[1:, cv2.CC_STAT_AREA]))
        mask = (part_labels == biggest).astype(np.uint8)
        # Полость окружена материалом разреза (за контуром её линии);
        # незаштрихованный прилив за секущей плоскостью — нет (0…0,2). У стенок
        # штриховка расчищена под размерами и штриховыми — отсюда 0,35 (корпус:
        # полость с «50» и «60» у стенок — 0,46).
        ring = cv2.dilate(mask, wide) & (1 - mask) & space & (1 - lines)
        if not ring.any() or float((ring & material).sum()) < 0.35 * float(ring.sum()):
            continue
        # Стрелки размеров, упёртые в стенку, выгрызают зубцы у края.
        notch = int(5 * line) | 1
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((notch, notch), np.uint8))
        mask = cv2.dilate(mask, grow)  # до середины линии контура полости
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            continue
        shape = max(contours, key=cv2.contourArea)
        bx, by, bw, bh = cv2.boundingRect(shape)
        if cv2.contourArea(shape) >= 0.6 * bw * bh:
            # Зазубрины и вырезы от стрелок и выносок размеров внутри полости —
            # не её форма (корпус 7: пустота Т-образная, рамка — точно 49 × 52).
            bx, by, bw, bh = _trim_to_walls(thick, bx, by, bw, bh, line)
            if strokes is None:
                from app.ai.cad_views.section_material import hatch_strokes, ink_mask

                strokes = hatch_strokes(ink_mask(crop, line), line)
            bx, by, bw, bh, walled = _grow_to_walls(thick, strokes, space, bx, by, bw, bh, line)
            poly = np.array([[bx, by], [bx + bw, by], [bx + bw, by + bh], [bx, by + bh]])
        else:
            walled = (False, False, False, False)
            poly = cv2.approxPolyDP(shape, 1.5 * line, True).reshape(-1, 2)
        points = _Void((float(px + x0), float(py + y0)) for px, py in poly)
        # Стороны, упёршиеся в стенку на листе (левая, верхняя, правая,
        # нижняя): их не двигает сверка с соседним видом.
        points.walled = walled
        # Куски одной полости, разрезанной размерной, дорастают до одних стенок.
        if any(
            len(other) == len(points)
            and all(abs(a - c) + abs(b - d) <= 2 * line for (a, b), (c, d) in zip(other, points))
            for other in polygons
        ):
            continue
        polygons.append(points)
    return polygons


def _grow_to_walls(
    thick: Any, material: Any, space: Any, bx: int, by: int, bw: int, bh: int, line: float
) -> tuple[int, int, int, int, tuple[bool, bool, bool, bool]]:
    """Рамка пустоты — наружу до стенки полости, если стенка рядом.

    Полость дробят основные линии размеров (стрелки «23», «10» внутри
    полости корпуса 8 — два куска вместо одного на 120,8). Наружу от каждой
    стороны ищется основная линия поперёк (не меньше 0,6 средней части
    стороны): штриховка стенки всегда за такой линией. Маской материала
    останавливаться нельзя — размерные внутри полости она метит штрихами
    (корпус 8: у «42» — 0,5 на полосе в 4 толщины)."""

    height, width = thick.shape[:2]
    band = int(line)
    rows = slice(by + bh // 5, by + 4 * bh // 5 + 1)
    cols = slice(bx + bw // 5, bx + 4 * bw // 5 + 1)

    def beyond_is_wall(v: int, step: int, vertical: bool) -> bool:
        """За стенкой полости — штриховка или конец тела вида; за размерной
        со стрелками внутри полости — та же пустота (корпус 8). Штриховка —
        по штрихам под 45°: маска материала метит и полосу между размерной
        и стенкой (корпус 19)."""
        a, b = sorted((v + step * int(2 * line), v + step * int(6 * line)))
        if vertical:
            if a < 0 or b >= width:
                return True
            window = material[rows, a : b + 1]
            open_ = space[rows, a : b + 1]
        else:
            if a < 0 or b >= height:
                return True
            window = material[a : b + 1, cols]
            open_ = space[a : b + 1, cols]
        if open_.size and open_.mean() < 0.5:
            return True  # дальше — не тело вида
        return bool(window.size) and float(window.mean()) >= 0.2

    def scan(start: int, stop: int, step: int, vertical: bool) -> int | None:
        for v in range(start, stop, step):
            if vertical:
                if not 0 <= v < width:
                    return None
                strip = thick[rows, max(0, v - band) : v + band + 1].any(axis=1)
                inside = space[rows, v]
            else:
                if not 0 <= v < height:
                    return None
                strip = thick[max(0, v - band) : v + band + 1, cols].any(axis=0)
                inside = space[v, cols]
            if strip.size and strip.mean() >= 0.45 and beyond_is_wall(v, step, vertical):
                # Порог срабатывает на внутреннем краю стенки (её рвут
                # размеры — 0,5), середина — на полтолщины дальше; за стенкой
                # штриховка тоже «основная», искать пик нельзя.
                if abs(v - start) <= line:
                    return start  # рамка уже на стенке
                return v + step * int(line / 2)
            if inside.size and not inside.any():
                return None
        return None

    left = scan(bx, max(-1, bx - bw), -1, True)
    right = scan(bx + bw, min(width, bx + 2 * bw), 1, True)
    top = scan(by, max(-1, by - bh), -1, False)
    bottom = scan(by + bh, min(height, by + 2 * bh), 1, False)
    x0 = left if left is not None else bx
    x1 = right if right is not None else bx + bw
    y0 = top if top is not None else by
    y1 = bottom if bottom is not None else by + bh
    walled = (left is not None, top is not None, right is not None, bottom is not None)
    return int(x0), int(y0), int(x1 - x0), int(y1 - y0), walled


def _trim_to_walls(
    thick: Any, bx: int, by: int, bw: int, bh: int, line: float
) -> tuple[int, int, int, int]:
    """Рамка пустоты — до стенки, если стенка прошла внутри рамки.

    Пустота утекает за дно полости туда, где штриховка расчищена под
    размерами («16», «20» под дном разреза корпуса), и рамка захватывала
    лишние 5 мм. Стенка — основная линия поперёк рамки у её края; её рвут
    стрелки размеров (дно корпуса 3: 304 px из 483), отсюда 0,6."""
    import cv2
    import numpy as np

    k = int(2 * line) | 1
    crop = thick[by : by + bh + 1, bx : bx + bw + 1]
    if crop.size == 0:
        return bx, by, bw, bh
    rows = cv2.morphologyEx(crop, cv2.MORPH_CLOSE, np.ones((1, k), np.uint8))
    cols = cv2.morphologyEx(crop, cv2.MORPH_CLOSE, np.ones((k, 1), np.uint8))
    from app.ai.cad_views.prismatic_parts import _longest_run

    margin = int(2 * line)
    long_rows = [
        i for i in range(margin, crop.shape[0] - margin) if _longest_run(rows[i]) >= 0.6 * bw
    ]
    long_cols = [
        j for j in range(margin, crop.shape[1] - margin) if _longest_run(cols[:, j]) >= 0.6 * bh
    ]
    top, bottom, left, right = by, by + bh, bx, bx + bw
    near = 0.3
    upper = [i for i in long_rows if i < near * bh]
    lower = [i for i in long_rows if i > (1 - near) * bh]
    if upper:
        top = by + max(upper)
    if lower:
        bottom = by + min(lower)
    first = [j for j in long_cols if j < near * bw]
    last = [j for j in long_cols if j > (1 - near) * bw]
    if first:
        left = bx + max(first)
    if last:
        right = bx + min(last)
    return left, top, right - left, bottom - top


def hidden_boxes(gray: Any, outline: Any) -> list[tuple[float, float, float, float]]:
    """Фигуры невидимого контура (тонкие штриховые) внутри контура вида.

    Штрихи смыкаются вдоль осей, и замкнутая внутренняя область — след
    полости или отверстия, видный насквозь; её рамка — пределы полости."""
    import cv2
    import numpy as np

    from app.ai.cad_views.extrude_body import main_line_mask

    line = outline.line
    pad = int(2 * line)
    crop, x0, y0 = _crop(gray, outline, pad)
    ink, thick, _ = main_line_mask(crop)
    thin = ink & (1 - cv2.dilate(thick, np.ones((3, 3), np.uint8)))
    k = max(3, int(8 * line))
    joined = cv2.morphologyEx(thin, cv2.MORPH_CLOSE, np.ones((1, k), np.uint8)) | cv2.morphologyEx(
        thin, cv2.MORPH_CLOSE, np.ones((k, 1), np.uint8)
    )
    # Углы: штрихи сторон не доходят друг до друга на пол-штриха.
    joined = cv2.dilate(joined, np.ones((int(line) | 1, int(line) | 1), np.uint8))
    contours, hierarchy = cv2.findContours(joined, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for index, contour in enumerate(contours):
        if hierarchy is None or hierarchy[0][index][3] == -1:
            continue  # внутренняя граница замкнутой фигуры
        bx, by, bw, bh = cv2.boundingRect(contour)
        if bw < 6 * line or bh < 6 * line:
            continue
        cx, cy = bx + x0 + bw / 2.0, by + y0 + bh / 2.0
        if not _inside(outline, cx, cy):
            continue
        boxes.append((float(bx + x0), float(by + y0), float(bw), float(bh)))
    return boxes


class _Points:
    """Контур без заливки — для перевода вершин в координаты детали."""

    def __init__(self, points: list[tuple[float, float]], box: tuple[int, int, int, int]):
        self.points = points
        self.box = box


def arrange_views(
    outlines: list[Any],
    main: Any,
    boxes: dict[int, tuple[float, ...]] | None = None,
    further: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Виды в проекционной связи с главным: {"below", "right", "above", "left"}.

    ``boxes`` — рамки тел видов (id контура → x, y, w, h): рамку контура
    раздувают прилипшие стрелки размеров (корпус 18: главный вид на 90 px
    выше тела — и вид слева «не той высоты»)."""
    boxes = boxes or {}
    mx, my, mw, mh = boxes.get(id(main), main.box)
    placed: dict[str, list[Any]] = {}
    for outline in outlines:
        if outline is main:
            continue
        x, y, w, h = boxes.get(id(outline), outline.box)
        columns = _overlap(mx, mx + mw, x, x + w) >= 0.8 * min(mw, w)
        rows = _overlap(my, my + mh, y, y + h) >= 0.8 * min(mh, h)
        if columns and abs(w - mw) <= 0.15 * mw:
            side = "below" if y > my + mh else "above" if y + h < my else None
        elif rows and abs(h - mh) <= 0.15 * mh:
            side = "right" if x > mx + mw else "left" if x + w < mx else None
        else:
            side = None
        if side is None:
            continue
        # Ближайший вид с каждой стороны (дальше — разрезы и повторные виды).
        gap = abs(
            (y - (my + mh))
            if side == "below"
            else (my - (y + h))
            if side == "above"
            else (x - (mx + mw))
            if side == "right"
            else (mx - (x + w))
        )
        placed.setdefault(side, []).append((gap, id(outline), outline))
    if further is not None:
        # Второй вид с той же стороны (вид спереди под разрезом корпуса):
        # на нём грань, снятая разрезом, — для формы приливов.
        for side, items in placed.items():
            if len(items) >= 2:
                further[side] = sorted(items, key=lambda item: item[0])[1][2]
    return {side: min(items, key=lambda item: item[0])[2] for side, items in placed.items()}


def view_polygon(
    outline: Any, side: str, scale: float, frame: dict[str, float]
) -> tuple[str, list[list[float]]]:
    """(ось взгляда, вершины контура в координатах детали) для вида."""
    x, y, w, h = outline.box
    points: list[list[float]] = []
    for px, py in outline.points:
        right = (px - x) * scale  # от левого края вида
        up = (y + h - py) * scale  # от нижнего края вида
        if side == "main":
            points.append([frame["x0"] + right, up])  # (X, Z)
        elif side == "below":
            points.append([frame["x0"] + right, up])  # (X, Y)
        elif side == "above":
            points.append([frame["x0"] + right, h * scale - up])  # (X, Y)
        elif side == "right":
            points.append([w * scale - right, up])  # (Y, Z)
        else:  # left
            points.append([right, up])  # (Y, Z)
    normal = {"main": "y", "below": "z", "above": "z", "right": "x", "left": "x"}[side]
    return normal, [[round(a, 4), round(b, 4)] for a, b in points]


def build_prismatic(
    gray: Any,
    reading: Any,
    label_texts: list[str],
    *,
    region_labels: dict[int, list[str]] | None = None,
    part: str | None = None,
) -> Any:
    """Призматическая деталь: брусок по габариту ∩ контуры видов."""
    from app.ai.cad_views.extrude_body import _match, outlines
    from app.ai.cad_views.labels import parse_label
    from app.ai.cad_views.pipeline import ViewsResult

    texts = list(label_texts)
    for extra in (region_labels or {}).values():
        texts.extend(extra)
    parsed = [parse_label(t) for t in texts]
    linear = [lab.value for lab in parsed if lab.kind == "linear" and lab.value]
    diameters = [lab.value for lab in parsed if lab.kind in ("diameter", "thread") and lab.value]
    # Роли областей модель путает (корпус живьём: план — «other», главным
    # назван кусок вида 60 × 110 px); контуры видов отбираются проекционной
    # связью, из областей отсекается только заведомо не изображение.
    not_pictures = ("title_block", "specification", "table", "notes", "label", "isometric")
    boxes = [tuple(r.box) for r in reading.regions if r.role not in not_pictures]
    if not boxes:
        return ViewsResult(False, "на листе не найдено изображения детали")
    # Стрелки размеров и цифры у кромок — толстые пятна: размыкание шире.
    # Отверстия крепежа на листе 1:2 — радиус 2,5…3 толщины линии: общий
    # порог (3) пропускал три из четырёх; мелкие проверяются стенками.
    found = outlines(gray, boxes, opening_lines=5.0, hole_min_lines=2.0)
    if len(found) < 2:
        return ViewsResult(False, "для призматической детали нужно не меньше двух видов")
    main_box = next((r.box for r in reading.regions if r.n == reading.main), None)

    def overlaps_main(outline: Any) -> float:
        if main_box is None:
            return 0.0
        x, y, w, h = outline.box
        bx0, by0, bx1, by1 = main_box
        return _overlap(x, x + w, bx0, bx1) * _overlap(y, y + h, by0, by1)

    from app.ai.cad_views.prismatic_parts import body_rect, line_masks

    _ink, thick, _thin, _line = line_masks(gray)
    candidates = sorted(found[:6], key=lambda o: (-overlaps_main(o), -o.box[2] * o.box[3]))
    from app.ai.cad_views.checks import label_coverage

    tried: list[str] = []
    options: list[Any] = []
    bodies = {}
    for outline in found[:6]:
        left, top, right, bottom = body_rect(thick, outline)
        bodies[id(outline)] = (left, top, right - left, bottom - top)
    for main in candidates[:3]:
        # Рамки тел точнее рамок контуров, но на реальном листе тело вида
        # находится не всегда (1c411279_p011/p012) — тогда по контурам.
        further: dict[str, Any] = {}
        views = arrange_views(found[:6], main, bodies, further)
        if not views:
            further = {}
            views = arrange_views(found[:6], main, None, further)
        if not views:
            tried.append(f"контур {main.box[2]}×{main.box[3]} px: нет видов в проекционной связи")
            continue
        rects = {side: body_rect(thick, o) for side, o in {"main": main, **views}.items()}
        extents = [rects["main"][2] - rects["main"][0], rects["main"][3] - rects["main"][1]]
        for side in views:
            left, top, right, bottom = rects[side]
            extents.append(bottom - top if side in ("below", "above") else right - left)
        fit = fit_edges_scale([main, *views.values()], linear, diameters, extents)
        if fit is None:
            tried.append(f"контур {main.box[2]}×{main.box[3]} px: надписи не объясняют кромок")
            continue
        scale, what = fit
        # Габарит ТЕЛА (без приливов) на каждом виде обязан быть надписан:
        # надписей на листе много, и масштаб «объяснял» 9 из них с видом
        # слева в роли главного, при теле 262 × 243 вместо 100 × 100.
        body_hits = 0
        for side in {"main": main, **views}:
            left, top, right, bottom = rects[side]
            sizes = [right - left, bottom - top]
            if side in ("below", "above"):
                sizes = [bottom - top]
            elif side in ("right", "left"):
                sizes = [right - left]
            for size in sizes:
                hit = _match(size * scale, linear, 0.03)
                if hit is not None:
                    body_hits += 1
                    what = [*what, f"тело {hit:g}"]
        tried.append(f"контур {main.box[2]}×{main.box[3]} px, {scale:.4f} мм/px: {', '.join(what)}")
        key = (len(views), body_hits, len(what))
        if len(what) >= 3:
            options.append((key, main, views, scale, what, further))
    # Каждая гипотеза «главный вид + масштаб» собирается целиком и сверяется
    # с надписями листа: признаки выбора (число видов, надписанный габарит)
    # на литом корпусе 1c411279_p011 выбирали вид с чужим масштабом.
    best = None
    for key, main, views, scale, what, further in sorted(options, key=lambda o: o[0], reverse=True)[
        :3
    ]:
        variants = [
            (
                1,
                _assemble(gray, main, views, scale, what, tried, linear, diameters, part, further),
            ),
            (0, _visual_hull(main, views, scale, what, tried, linear, diameters, part)),
        ]
        for priority, result in variants:
            if not result.ok:
                continue
            share = float(label_coverage(result, texts).get("share") or 0.0)
            # Пересечение контуров — запасная гипотеза: берётся, только если
            # объясняет лист заметно лучше (на корпусах надписей у обеих
            # поровну, а тело без полости и приливов — не та деталь).
            disagree = int((result.scales or {}).get("views_disagree") or 0)
            rank = (-disagree, round(share - (0.0 if priority else 0.1), 2), priority, key)
            if best is None or rank > best[0]:
                best = (rank, result)
    if best is None:
        return ViewsResult(
            False,
            "призматическая деталь не объяснена видами и надписями: " + "; ".join(tried),
        )
    return best[1]


def _feature_lengths(
    features: list[dict[str, Any]], extent: dict[str, float] | None = None
) -> list[float]:
    """Размеры элементов, как их надписывают: стороны полостей и приливов,
    глубина, вылет; положения центров от граней тела и шаг между центрами
    («11» от кромки, «58» между отверстиями крепежа)."""
    lengths: list[float] = []
    centres: dict[str, list[float]] = {"x": [], "y": [], "z": []}
    for feature in features:
        placement = (feature.get("params") or {}).get("placement") or {}
        origin, axis = placement.get("origin"), placement.get("axis")
        if not origin or not axis or extent is None:
            continue
        for index, name in enumerate(("x", "y", "z")):
            if abs(axis[index]) > 0.5 or name not in extent:
                continue
            value = float(origin[index])
            centres[name].append(value)
            lengths += [value, extent[name] - value]
    for values in centres.values():
        values = sorted(set(round(v, 2) for v in values))
        lengths += [b - a for i, a in enumerate(values) for b in values[i + 1 :]]
    for feature in features:
        params = feature.get("params") or {}
        if feature.get("kind") == "cut_prism":
            points = params.get("polygon_mm") or []
            if points:
                lengths.append(max(p[0] for p in points) - min(p[0] for p in points))
                lengths.append(max(p[1] for p in points) - min(p[1] for p in points))
            low, high = params.get("range_mm") or (0.0, 0.0)
            lengths.append(high - low)
        elif feature.get("kind") == "boss":
            lengths.append(float(params.get("depth_mm") or 0.0))
            for key in ("width_mm", "height_mm"):
                if params.get(key):
                    lengths.append(float(params[key]))
    return [round(v, 3) for v in lengths if v > 0]


def _axis_line(
    thin: Any,
    frame: Any,
    tangent: str,
    centre: float,
    axis: str,
    face: float,
    sign: int,
    depth: float,
) -> float:
    """Доля осевой линии прилива на виде сбоку: тонкая по центру пролёта от
    грани тела до торца и дальше за торец (ГОСТ 2.303 — ось выходит за
    контур на 2…5 мм)."""
    from app.ai.cad_views.prismatic_parts import _segment_samples

    reach = depth + 3.0
    low, high = sorted((face, face + sign * reach))
    samples = _segment_samples(thin, frame, {tangent: centre}, axis, low, high)
    return sum(samples) / len(samples) if samples else 0.0


def _face_pockets(
    gray: Any,
    frames: dict[str, Any],
    thick: Any,
    thin: Any,
    extent: dict[str, float],
    cavities: list[dict[str, Any]],
    bosses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Карманы на гранях тела по прямоугольникам видов (см. вызов)."""
    from app.ai.cad_views.prismatic_parts import cavity_span, visible_rectangles

    def cavity_box(cavity: dict[str, Any], u: str, v: str) -> tuple[float, ...] | None:
        params = cavity["params"]
        a, b = _KERNEL_PLANE[params["normal"]]
        points = params["polygon_mm"]
        spans = {
            a: (min(p[0] for p in points), max(p[0] for p in points)),
            b: (min(p[1] for p in points), max(p[1] for p in points)),
            params["normal"]: tuple(params["range_mm"]),
        }
        return (*spans[u], *spans[v])

    found: list[dict[str, Any]] = []
    seen: list[tuple[str, float, float, float, float, float]] = []
    for frame in frames.values():
        if frame.section:
            continue
        n = frame.normal
        (ua, _), (va, _) = frame.u, frame.v
        boxes = [(box, True) for box in visible_rectangles(thick, frame)]
        boxes += [(box, False) for box in hidden_boxes(gray, frame.outline)]
        for (bx, by, bw, bh), visible in boxes:
            corner_a, corner_b = frame.to_part(bx, by), frame.to_part(bx + bw, by + bh)
            u0, u1 = sorted((corner_a[ua], corner_b[ua]))
            v0, v1 = sorted((corner_a[va], corner_b[va]))
            if u1 - u0 < 3.0 or v1 - v0 < 3.0:
                continue
            # Полость, видная насквозь, и торцы приливов — уже построены.
            covered = False
            for cavity in cavities:
                box = cavity_box(cavity, ua, va)
                if box is None:
                    continue
                inter = _overlap(u0, u1, box[0], box[1]) * _overlap(v0, v1, box[2], box[3])
                if inter >= 0.5 * (u1 - u0) * (v1 - v0):
                    covered = True
            for boss in bosses:
                if (
                    boss["axis"] == n
                    and u0 <= boss["origin"][ua] <= u1
                    and v0 <= boss["origin"][va] <= v1
                ):
                    covered = True
            if covered:
                continue
            near_min = frame.near == "min"
            on_min = near_min if visible else not near_min
            face = 0.0 if on_min else extent[n]
            depth = None
            for other in frames.values():
                if other is frame or other.section or n not in (other.u[0], other.v[0]):
                    continue
                shared = other.u[0] if other.v[0] == n else other.v[0]
                lo, hi = (u0, u1) if shared == ua else (v0, v1)
                span = cavity_span(thin, other, n, shared, lo, hi, extent[n], extent[shared])
                if span is None:
                    continue
                a, b = span[0], span[1]
                line_mm = 2 * other.outline.line * other.scale
                if abs(a - face) <= line_mm:
                    depth = b - a
                elif abs(b - face) <= line_mm:
                    depth = b - a
                if depth is not None:
                    break
            if depth is None or depth <= 0.5 or depth >= 0.9 * extent[n]:
                continue
            key = (n, round(face, 1), round(u0), round(u1), round(v0), round(v1))
            if key in seen:
                continue
            seen.append(key)
            origin = {n: face, ua: (u0 + u1) / 2.0, va: (v0 + v1) / 2.0}
            found.append(
                {
                    "kind": "pocket",
                    "params": {
                        "placement": {
                            "origin": _point(origin),
                            "axis": _vector(n, 1.0 if on_min else -1.0),
                            "ref": _vector(ua),
                        },
                        "profile": "rectangle",
                        "width_mm": round(u1 - u0, 3),
                        "height_mm": round(v1 - v0, 3),
                        "depth_mm": round(depth, 3),
                    },
                    "confidence": 0.4,
                }
            )
    return found


def _snap_length(value: float, labels: list[float], share: float = 0.03) -> float:
    from app.ai.cad_views.extrude_body import _match

    hit = _match(value, labels, share)
    return hit if hit is not None and abs(hit - value) <= max(1.0, share * hit) else value


def _snap_interval(
    low: float, high: float, size: float, labels: list[float], near: float = 1.0
) -> tuple[float, float]:
    """Отрезок элемента по оси тела [0, size]: длина — по надписи, край у
    грани — на грани, середина у середины тела — посередине, иначе край —
    на надписанном удалении от ближней грани."""
    length = _snap_length(high - low, labels)
    centre = (low + high) / 2.0
    if abs(low) <= near:
        return 0.0, length
    if abs(high - size) <= near:
        return size - length, size
    if abs(centre - size / 2.0) <= near:
        return size / 2.0 - length / 2.0, size / 2.0 + length / 2.0
    offset = _snap_length(low, labels, 0.05)
    if abs(offset - low) <= near:
        return offset, offset + length
    far = _snap_length(size - high, labels, 0.05)
    if abs(far - (size - high)) <= near:
        return size - far - length, size - far
    return centre - length / 2.0, centre + length / 2.0


def _nominal_features(
    features: list[dict[str, Any]], extent: dict[str, float], linear: list[float]
) -> None:
    for feature in features:
        params = feature.get("params") or {}
        kind = feature.get("kind")
        if kind == "cut_prism":
            n = params["normal"]
            a, b = _KERNEL_PLANE[n]
            points = params["polygon_mm"]
            if len(points) == 4:
                a0, a1 = _snap_interval(
                    min(p[0] for p in points), max(p[0] for p in points), extent[a], linear
                )
                b0, b1 = _snap_interval(
                    min(p[1] for p in points), max(p[1] for p in points), extent[b], linear
                )
                params["polygon_mm"] = [
                    [round(a0, 4), round(b0, 4)],
                    [round(a1, 4), round(b0, 4)],
                    [round(a1, 4), round(b1, 4)],
                    [round(a0, 4), round(b1, 4)],
                ]
            r0, r1 = params["range_mm"]
            r0, r1 = _snap_interval(r0, r1, extent[n], linear)
            params["range_mm"] = [round(r0, 4), round(r1, 4)]
        elif kind in ("boss", "pocket"):
            params["depth_mm"] = round(_snap_length(float(params["depth_mm"]), linear, 0.08), 3)
            for key in ("width_mm", "height_mm"):
                if params.get(key):
                    params[key] = round(_snap_length(float(params[key]), linear), 3)
        if kind in ("boss", "pocket", "hole"):
            placement = params.get("placement") or {}
            origin = placement.get("origin")
            axis = placement.get("axis")
            if not origin or not axis:
                continue
            for index, name in enumerate(("x", "y", "z")):
                if abs(axis[index]) > 0.5 or name not in extent:
                    continue  # вдоль оси элемента — грань, её не трогаем
                size = extent[name]
                value = origin[index]
                if abs(value - size / 2.0) <= 0.6:
                    origin[index] = round(size / 2.0, 4)
                    continue
                edge = min(value, size - value)
                snapped = _snap_length(edge, linear, 0.06)
                if abs(snapped - edge) <= 0.6:
                    origin[index] = round(snapped if value < size / 2.0 else size - snapped, 4)


def _visual_hull(
    main: Any,
    views: dict[str, Any],
    scale: float,
    what: list[str],
    tried: list[str],
    linear: list[float],
    diameters: list[float],
    part: str | None,
) -> Any:
    """Прежняя гипотеза: брусок по наибольшему габариту ∩ контуры видов.

    Литой корпус из цилиндров и шестигранников (1c411279_p011) тела-бруска
    с приливами не имеет, и сборка «тело + элементы» объясняла 10 надписей
    из 26, а пересечение контуров — 20. Обе гипотезы сверяются с листом."""
    from app.ai.cad_views.extrude_body import _match
    from app.ai.cad_views.pipeline import ViewsResult

    mx, my, mw, mh = main.box
    widths = [mw] + [views[k].box[2] for k in ("below", "above") if k in views]
    heights = [mh] + [views[k].box[3] for k in ("right", "left") if k in views]
    depths = [views[k].box[3] for k in ("below", "above") if k in views] + [
        views[k].box[2] for k in ("right", "left") if k in views
    ]
    # Заготовка — по наибольшему габариту: форму задают пересечения с
    # контурами видов, а выступ стрелки на одном виде срезает контур другого;
    # по наименьшему терялись приливы, видные не на всех видах.
    width, height, depth = (
        max(widths) * scale,
        max(heights) * scale,
        max(depths) * scale,
    )
    width = _match(width, linear, 0.03) or round(width, 3)
    height = _match(height, linear, 0.03) or round(height, 3)
    depth = _match(depth, linear, 0.03) or round(depth, 3)
    base = {
        "kind": "extrude",
        "params": {
            "sketch_profile": [
                {"kind": "line", "to": [width, 0.0]},
                {"kind": "line", "to": [width, depth]},
                {"kind": "line", "to": [0.0, depth]},
                {"kind": "line", "to": [0.0, 0.0]},
            ],
            "depth_mm": height,
        },
        "confidence": 0.6,
    }
    features: list[dict[str, Any]] = [base]
    frame = {"x0": 0.0}
    used = {"main": main, **views}
    polygons: list[dict[str, Any]] = []
    for side, outline in used.items():
        normal, polygon = view_polygon(outline, side, scale, frame)
        polygons.append({"normal": normal, "points": polygon})
        features.append(
            {
                "kind": "intersect",
                "params": {"normal": normal, "polygon_mm": polygon},
                "confidence": 0.6,
            }
        )
    holes = []
    for side, outline in used.items():
        x, y, w, h = outline.box
        for cx, cy, r in outline.holes:
            right, up = (cx - x) * scale, (y + h - cy) * scale
            diameter = _match(2 * r * scale, diameters, 0.06) or round(2 * r * scale, 3)
            if side == "main":
                origin, axis = [right, -1.0, up], [0.0, 1.0, 0.0]
            elif side in ("below", "above"):
                yy = up if side == "below" else h * scale - up
                origin, axis = [right, yy, height + 1.0], [0.0, 0.0, -1.0]
            elif side == "right":
                origin, axis = [-1.0, w * scale - right, up], [1.0, 0.0, 0.0]
            else:
                origin, axis = [-1.0, right, up], [1.0, 0.0, 0.0]
            holes.append(
                {
                    "kind": "hole",
                    "params": {
                        "placement": {
                            "origin": [round(v, 4) for v in origin],
                            "axis": axis,
                            "ref": [0.0, 0.0, 1.0] if axis[2] == 0 else [1.0, 0.0, 0.0],
                        },
                        "diameter_mm": diameter,
                        "through": True,
                    },
                    "confidence": 0.5,
                    "view": side,
                }
            )
    # Одно отверстие видно окружностью только на одном виде — дубли
    # (одинаковая ось и центр) не повторяются.
    unique: list[dict[str, Any]] = []
    for hole in holes:
        placement = hole["params"]["placement"]
        if any(
            other["params"]["placement"]["axis"] == placement["axis"]
            and sum(
                abs(a - b)
                for a, b in zip(other["params"]["placement"]["origin"], placement["origin"])
            )
            < 1.0
            for other in unique
        ):
            continue
        unique.append(hole)
    features.extend({k: v for k, v in hole.items() if k != "view"} for hole in unique)
    candidate = {
        "candidate": {"features": features, "score": 0.6, "label": part or "деталь"},
        "confirm_assumptions": True,
        "metadata": {"source": "cad_views", "method": "prismatic"},
    }
    return ViewsResult(
        True,
        candidate=candidate,
        profile={
            "kind": "prismatic",
            "bounds_mm": [width, depth, height],
            "views": sorted(used),
            "polygons": polygons,
            "source_box": [mx, my, mx + mw, my + mh],
        },
        features=[
            {"kind": "hole", "diameter_mm": h["params"]["diameter_mm"], "view": h["view"]}
            for h in unique
        ],
        scales={"mm_per_px": scale, "labels_explained": len(what)},
        notes=["виды: " + ", ".join(sorted(used)), "контур: " + "; ".join(tried)],
    )


def is_section(gray: Any, outline: Any) -> bool:
    """Вид — разрез: штриховка занимает заметную часть контура."""
    import numpy as np

    from app.ai.cad_views.section_material import section_material

    line = outline.line
    crop, x0, y0 = _crop(gray, outline, int(2 * line))
    material, _axis = section_material(crop, line, revolve=False, main_boundary=_MAIN_BOUNDARY)
    fx, fy = outline.filled_origin
    fh, fw = outline.filled.shape
    region = np.zeros_like(material)
    oy, ox = fy - y0, fx - x0
    ys = slice(max(0, oy), min(region.shape[0], oy + fh))
    xs = slice(max(0, ox), min(region.shape[1], ox + fw))
    region[ys, xs] = outline.filled[
        ys.start - oy : ys.stop - oy, xs.start - ox : xs.stop - ox
    ].astype(region.dtype)
    total = float(region.sum())
    return total > 0 and float((material & region).sum()) >= 0.15 * total


_KERNEL_PLANE = {"z": ("x", "y"), "y": ("x", "z"), "x": ("y", "z")}
_UNIT = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}


def _vector(axis: str, sign: float = 1.0) -> list[float]:
    return [sign * v for v in _UNIT[axis]]


def _point(values: dict[str, float]) -> list[float]:
    return [round(values.get(a, 0.0), 4) for a in ("x", "y", "z")]


def _section_cavities(
    gray: Any,
    frames: dict[str, Any],
    thick: Any,
    thin: Any,
    ink: Any,
    extent: dict[str, float],
) -> list[dict[str, Any]]:
    """Вырезы полостей по разрезам: сечение пустоты на разрезе, пределы вдоль
    его взгляда — по невидимому контуру на соседнем виде.

    Разрез в проекционной связи читается как вид на его месте (ГОСТ 2.305).
    Направление по согласию с соседними видами пробовалось и отвергнуто: на
    листах с верной ориентацией голосование переворачивало разрез (корпус 7)
    — линий размеров и штриховых слишком много, чтобы совпадения что-то
    значили."""
    from app.ai.cad_views.prismatic_parts import cavity_span

    main = frames.get("main")
    features: list[dict[str, Any]] = []
    for frame in frames.values():
        if not frame.section:
            continue
        n = frame.normal
        free = [
            a for a in (frame.u[0], frame.v[0]) if main is None or a not in (main.u[0], main.v[0])
        ]
        voids = section_voids(gray, frame.outline, frame.rect, thick)

        def measure(void: list[tuple[float, float]]) -> tuple[list[dict[str, float]], Any]:
            points = [frame.to_part(px, py) for px, py in void]
            spans = []
            for other in frames.values():
                if other is frame or other.section or n not in (other.u[0], other.v[0]):
                    continue
                shared = other.u[0] if other.v[0] == n else other.v[0]
                if free and shared == free[0]:
                    continue  # знак этой оси ещё не известен
                v0 = min(p[shared] for p in points)
                v1 = max(p[shared] for p in points)
                span = cavity_span(thin, other, n, shared, v0, v1, extent[n], extent[shared])
                if span is None:
                    continue
                r0, r1, s0, s1 = span
                # Пролёт по общей оси — по концам штриховых соседнего вида,
                # если пустота разреза прямоугольная (её режут размеры).
                if len(points) == 4 and s1 - s0 <= 1.6 * (v1 - v0) + 2.0:
                    low_px = frame.to_sheet({**points[0], shared: v0})
                    high_px = frame.to_sheet({**points[0], shared: v1})
                    walled = getattr(void, "walled", (False,) * 4)

                    def fixed(px_point: tuple[float, float]) -> bool:
                        # Сторона листа, где лежит этот край пролёта.
                        xs = [p[0] for p in void]
                        ys = [p[1] for p in void]
                        if abs(px_point[0] - min(xs)) < 1.0:
                            return walled[0]
                        if abs(px_point[0] - max(xs)) < 1.0:
                            return walled[2]
                        if abs(px_point[1] - min(ys)) < 1.0:
                            return walled[1]
                        return walled[3]

                    new_low = v0 if fixed(low_px) else s0
                    new_high = v1 if fixed(high_px) else s1
                    for point in points:
                        if abs(point[shared] - v0) < 1e-6:
                            point[shared] = new_low
                        elif abs(point[shared] - v1) < 1e-6:
                            point[shared] = new_high
                spans.append((r0, r1))
            return points, (spans[0] if spans else None)

        for void in voids:
            points, span = measure(void)
            if span is None:
                continue
            r0, r1 = span
            a, b = _KERNEL_PLANE[n]
            features.append(
                {
                    "kind": "cut_prism",
                    "params": {
                        "normal": n,
                        "polygon_mm": [[round(p[a], 4), round(p[b], 4)] for p in points],
                        "range_mm": [round(r0, 4), round(r1, 4)],
                    },
                    "confidence": 0.5,
                }
            )
    return features


def _extend_to_body(frames: dict[str, Any], lines: dict[str, Any]) -> None:
    """Размер тела по оси — тот, что подтверждают линии на всех его видах.

    Кромку тела на одном виде рвут приливы и стрелки размеров (разрез
    корпуса: левая кромка прервана приливом — «тело» сжималось до правой
    стенки; вид слева: крайней стала размерная со стрелками), а у уголка верх
    стойки на главном виде — короткая линия. Кандидаты — размеры тела по
    видам; побеждает тот, при котором на большем числе видов на обоих концах
    есть линия; вид достраивается к линии нужного места."""
    for axis in ("x", "y", "z"):
        members = {
            side: frame for side, frame in frames.items() if axis in (frame.u[0], frame.v[0])
        }
        if not members:
            continue

        def ends(side: str) -> tuple[bool, float, float, list[tuple[float, float]]]:
            frame = members[side]
            left, top, right, bottom = frame.rect
            horizontal = frame.u[0] == axis
            coords = lines[side]["cols" if horizontal else "rows"]
            low, high = (left, right) if horizontal else (top, bottom)
            return horizontal, low, high, coords

        def placements(side: str, size_mm: float) -> list[tuple[float, float, float, float]]:
            """(зазор, начало, конец, доля более короткой из двух линий)."""
            frame = members[side]
            _horizontal, low, high, coords = ends(side)
            tolerance = 2 * frame.outline.line
            want = size_mm / frame.scale
            found = []
            for a, b in ((low, low + want), (high - want, high)):
                near_a = [(abs(x - a), share) for x, share in coords if abs(x - a) <= tolerance]
                near_b = [(abs(x - b), share) for x, share in coords if abs(x - b) <= tolerance]
                if near_a and near_b:
                    gap_a, share_a = min(near_a)
                    gap_b, share_b = min(near_b)
                    found.append((gap_a + gap_b, a, b, min(share_a, share_b)))
            return found

        sizes = {side: (ends(side)[2] - ends(side)[1]) * members[side].scale for side in members}
        best = None
        for size in sorted(set(round(v, 3) for v in sizes.values())):
            # Опора — длина линий на концах: кромка тела длинная, торец
            # прилива (корпус: 108 вместо 100 по кромке прилива) — короткий.
            support = sum(
                max((option[3] for option in placements(side, size)), default=0.0)
                for side in members
            )
            key = (round(support, 2), size)
            if best is None or key > best[0]:
                best = (key, size)
        if best is None:
            continue
        size = best[1]
        for side, frame in members.items():
            if abs(sizes[side] - size) <= 2 * frame.outline.line * frame.scale:
                continue
            options = placements(side, size)
            if not options:
                continue
            _gap, new_low, new_high, _share = min(options)
            left, top, right, bottom = frame.rect
            horizontal = frame.u[0] == axis
            frame.rect = (
                (new_low, top, new_high, bottom) if horizontal else (left, new_low, right, new_high)
            )


def _assemble(
    gray: Any,
    main: Any,
    views: dict[str, Any],
    scale: float,
    what: list[str],
    tried: list[str],
    linear: list[float],
    diameters: list[float],
    part: str | None,
    further: dict[str, Any] | None = None,
) -> Any:
    """Тело по кромкам видов + выступы → приливы, окружности → отверстия,
    пустоты разрезов → полости."""
    import statistics

    import cv2
    import numpy as np

    from app.ai.cad_views.extrude_body import _match
    from app.ai.cad_views.pipeline import ViewsResult
    from app.ai.cad_views.prismatic_parts import (
        ViewFrame,
        body_rect,
        edge_lines,
        line_extent,
        line_masks,
        protrusions,
        ring_cover,
    )

    ink, thick, _thin, _line = line_masks(gray)
    used = {"main": main, **views}
    lines = {side: edge_lines(thick, outline) for side, outline in used.items()}
    frames = {
        side: ViewFrame(
            side, outline, body_rect(thick, outline, lines[side]), scale, is_section(gray, outline)
        )
        for side, outline in used.items()
    }
    _extend_to_body(frames, lines)
    # Масштаб — по габаритам тела: подбор по кромкам берёт первую пару
    # «надпись / расстояние», и та бывает неточной на процент-полтора
    # (корпус 8: 152,5 при 150 — тело прилипало к надписи полости 50,8).
    ratios = []
    for frame in frames.values():
        left, top, right, bottom = frame.rect
        for pixels in (right - left, bottom - top):
            label = _match(pixels * scale, linear, 0.03)
            if label is not None and pixels > 0:
                ratios.append(label / pixels)
    if ratios:
        ratios.sort()
        scale = ratios[len(ratios) // 2]
        for frame in frames.values():
            frame.scale = scale

    def size(frame: ViewFrame, axis: str) -> float:
        left, top, right, bottom = frame.rect
        return ((right - left) if frame.u[0] == axis else (bottom - top)) * scale

    extent: dict[str, float] = {}
    # Виды, не согласные о размере тела по оси, — признак неверной гипотезы
    # раскладки (корпус 23: вид «над» главным при главном-разрезе, 60 и 80).
    disagreement = 0
    for axis in ("x", "y", "z"):
        values = sorted(size(f, axis) for f in frames.values() if axis in (f.u[0], f.v[0]))
        if not values:
            continue
        # Медиана (у двух видов — середина): вид с прилипшей линией давал
        # 70,8 при 69,8 у соседнего, и тело брало надпись полости.
        value = statistics.median(values)
        if max(values) - min(values) > 0.03 * max(values):
            disagreement += 1
        extent[axis] = _match(value, linear, 0.03) or round(value, 3)
    if "y" not in extent:
        return ViewsResult(False, "глубина детали не видна ни на одном виде")
    width, depth, height = extent["x"], extent["y"], extent["z"]
    features: list[dict[str, Any]] = [
        {
            "kind": "extrude",
            "params": {
                "sketch_profile": [
                    {"kind": "line", "to": [width, 0.0]},
                    {"kind": "line", "to": [width, depth]},
                    {"kind": "line", "to": [0.0, depth]},
                    {"kind": "line", "to": [0.0, 0.0]},
                ],
                "depth_mm": height,
            },
            "confidence": 0.6,
        }
    ]
    notes: list[str] = []

    # Вырезы в очертании тела (уступы, уголок): тело ∩ контур вида в его
    # кромках. Выступы за кромки — приливы, ниже.
    polygons: list[dict[str, Any]] = []
    for side, frame in frames.items():
        outline = frame.outline
        fx, fy = outline.filled_origin
        left, top, right, bottom = (int(round(v)) for v in frame.rect)
        inside = np.zeros_like(outline.filled, dtype=np.uint8)
        inside[
            max(0, top - fy) : max(0, bottom - fy + 1), max(0, left - fx) : max(0, right - fx + 1)
        ] = 1
        body = (outline.filled.astype(np.uint8) & inside).astype(np.uint8)
        rect_area = float(inside.sum()) or 1.0
        a, b = _KERNEL_PLANE[frame.normal]
        contours, _ = cv2.findContours(body, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            continue
        shape = max(contours, key=cv2.contourArea)
        poly = cv2.approxPolyDP(shape, 1.0 * outline.line, True).reshape(-1, 2)
        points = [frame.to_part(px + fx, py + fy) for px, py in poly]
        polygon = [[round(p[a], 4), round(p[b], 4)] for p in points]
        # Для сверки с надписями и показа — контур вида целиком, с приливами:
        # их вылеты тоже надписаны.
        _normal, whole = view_polygon(outline, side, scale, {"x0": 0.0})
        polygons.append({"normal": frame.normal, "points": whole})
        if float(body.sum()) >= 0.97 * rect_area or len(polygon) < 3:
            continue
        features.append(
            {
                "kind": "intersect",
                "params": {"normal": frame.normal, "polygon_mm": polygon},
                "confidence": 0.6,
            }
        )

    # Полости по разрезу (заодно — направление взгляда разрезов, от него
    # зависят и выступы на них): вырезы идут после приливов.
    cavity_features = _section_cavities(gray, frames, thick, _thin, ink, extent)
    cavities = len(cavity_features)

    # Приливы: выступ за кромку на двух видах, форма — на виде вдоль оси.
    candidates: list[dict[str, Any]] = []
    for side, frame in frames.items():
        for item in protrusions(frame.outline, frame.rect, thick):
            (ua, us), (va, vs) = frame.u, frame.v
            if item["side"] in ("left", "right"):
                axis, sign = ua, (us if item["side"] == "right" else -us)
                tangent = va
            else:
                axis, sign = va, (vs if item["side"] == "top" else -vs)
                tangent = ua
            low, high = frame.span(tangent, item["a0"], item["a1"])
            candidates.append(
                {
                    "axis": axis,
                    "sign": sign,
                    "tangent": tangent,
                    "range": (low, high),
                    "depth": item["depth"] * scale,
                    "view": side,
                }
            )
    along_of = {f.normal: f for f in frames.values() if not f.section}
    for side, outline in (further or {}).items():
        # Вид за разрезом в том же ряду: на нём снятая разрезом грань.
        if is_section(gray, outline):
            continue
        far = ViewFrame(side, outline, body_rect(thick, outline), scale)
        along_of.setdefault(far.normal, far)
    along_any = {f.normal: f for f in frames.values()}
    paired: set[int] = set()
    bosses: list[dict[str, Any]] = []
    for i, one in enumerate(candidates):
        if i in paired:
            continue
        partner = None
        for j, two in enumerate(candidates):
            if j == i or j in paired or two["view"] == one["view"]:
                continue
            if (two["axis"], two["sign"]) != (one["axis"], one["sign"]):
                continue
            if two["tangent"] == one["tangent"]:
                continue
            if abs(two["depth"] - one["depth"]) > 0.3 * max(one["depth"], two["depth"]) + 1.0:
                continue
            partner = j
            break
        axis, sign = one["axis"], one["sign"]
        t1, (a0, a1) = one["tangent"], one["range"]
        others = [ax for ax in ("x", "y", "z") if ax not in (axis, t1)]
        t2 = others[0]
        if partner is not None:
            paired.update({i, partner})
            b0, b1 = candidates[partner]["range"]
            boss_depth = (one["depth"] + candidates[partner]["depth"]) / 2.0
        else:
            paired.add(i)
            b0 = b1 = None
            boss_depth = one["depth"]
        face = extent[axis] if sign > 0 else 0.0
        c1 = (a0 + a1) / 2.0
        d1 = a1 - a0
        profile = None
        along = along_of.get(axis) or along_any.get(axis)
        if along is not None:
            # Окружность диаметром пролёта на виде вдоль оси прилива.
            centres = (
                [(b0 + b1) / 2.0]
                if b0 is not None
                else [
                    k * scale
                    for k in range(0, int(extent[t2] / scale) + 1, max(1, int(along.outline.line)))
                ]
            )
            best = (0.0, None)
            for c2 in centres:
                px, py = along.to_sheet({t1: c1, t2: c2})
                cover = ring_cover(ink, px, py, d1 / 2.0 / scale, along.outline.line)
                if cover > best[0]:
                    best = (cover, c2)
            # Без второго вида выступ подтверждает только уверенная окружность:
            # иначе пятна стрелок у кромки становились приливами Ø5.
            if best[0] >= (0.8 if b0 is None else 0.75) or (
                b0 is not None and best[0] >= 0.6 and abs((b1 - b0) - d1) <= 0.15 * d1 + 1.0
            ):
                profile = "circle"
                c2 = best[1]
        if profile is None and b0 is not None and abs((b1 - b0) - d1) <= 0.15 * d1 + 1.0:
            # Вида вдоль оси нет (там разрез) — цилиндр выдаёт осевая
            # штрихпунктирная на обоих видах, где прилив виден выступом.
            partner_item = candidates[partner] if partner is not None else None
            marks = [
                _axis_line(
                    _thin, frames[item["view"]], item["tangent"], c, axis, face, sign, depth_mm
                )
                for item, c in ((one, c1), (partner_item, (b0 + b1) / 2.0))
                if item is not None
                for depth_mm in (boss_depth,)
            ]
            if marks and min(marks) >= 0.5:
                profile = "circle"
                c2 = (b0 + b1) / 2.0
                d1 = (d1 + (b1 - b0)) / 2.0
        if profile is None:
            if b0 is None:
                continue  # второго пролёта нет и формы не видно — не угадываем
            profile = "rectangle"
            c2 = (b0 + b1) / 2.0
        origin = {axis: face, t1: c1, t2: c2}
        params: dict[str, Any] = {
            "placement": {
                "origin": _point(origin),
                "axis": _vector(axis, sign),
                "ref": _vector(t1),
            },
            "profile": profile,
            "depth_mm": round(boss_depth, 3),
        }
        if profile == "circle":
            diameter = _match(d1, diameters, 0.06) or round(d1, 3)
            params["diameter_mm"] = diameter
        else:
            params["width_mm"] = round(d1, 3)
            params["height_mm"] = round(b1 - b0, 3)  # type: ignore[operator]
        bosses.append(
            {"axis": axis, "sign": sign, "origin": origin, "profile": profile, "size": d1}
        )
        features.append({"kind": "boss", "params": params, "confidence": 0.5})
    if bosses:
        notes.append(f"приливов: {len(bosses)}")

    features.extend(cavity_features)
    if cavities:
        notes.append(f"полостей по разрезу: {cavities}")

    # Карманы стенок: прямоугольник на виде грани (видимый — на ближней
    # грани, невидимый — на дальней), глубина — по соседнему виду: дно
    # кармана — линия поперёк у грани в том же пролёте.
    pockets = _face_pockets(gray, frames, thick, _thin, extent, cavity_features, bosses)
    features.extend(pockets)
    if pockets:
        notes.append(f"карманов стенок: {len(pockets)}")

    # Отверстия: окружность на виде; видимая — от ближней грани, невидимая —
    # от дальней; глубина — по паре линий стенок на соседних видах.
    holes: list[dict[str, Any]] = []
    for side, frame in frames.items():
        if frame.section:
            continue
        n = frame.normal
        (ua, _), (va, _) = frame.u, frame.v
        left, top, right, bottom = frame.rect
        for cx, cy, r in frame.outline.holes:
            if not (left < cx < right and top < cy < bottom):
                continue
            line = frame.outline.line
            visible = ring_cover(thick, cx, cy, r, line) >= 0.5
            if not visible and ring_cover(ink, cx, cy, r, line) < 0.5:
                continue
            centre = frame.to_part(cx, cy)
            radius_mm = r * scale
            if any(
                boss["axis"] == n
                and abs(boss["origin"][ua] - centre[ua]) < radius_mm
                and abs(boss["origin"][va] - centre[va]) < radius_mm
                for boss in bosses
            ):
                continue  # торец прилива на виде вдоль его оси
            # Выступ того же пролёта на виде сбоку — прилив, а не отверстие,
            # даже если прилив не собрался (корпус 21: торец Ø25 сверлился
            # насквозь отверстием Ø25).
            face_boss = next(
                (
                    item
                    for item in candidates
                    if item["axis"] == n
                    and item["range"][0] - radius_mm * 0.2
                    <= centre[item["tangent"]]
                    <= item["range"][1] + radius_mm * 0.2
                    and abs((item["range"][1] - item["range"][0]) - 2 * radius_mm)
                    <= 0.2 * 2 * radius_mm
                ),
                None,
            )
            if face_boss is not None:
                if not any(
                    boss["axis"] == n for boss in bosses if boss["sign"] == face_boss["sign"]
                ):
                    face = extent[n] if face_boss["sign"] > 0 else 0.0
                    origin = {**centre, n: face}
                    diameter = _match(2 * radius_mm, diameters, 0.06) or round(2 * radius_mm, 3)
                    bosses.append(
                        {
                            "axis": n,
                            "sign": face_boss["sign"],
                            "origin": origin,
                            "profile": "circle",
                            "size": diameter,
                        }
                    )
                    features.append(
                        {
                            "kind": "boss",
                            "params": {
                                "placement": {
                                    "origin": _point(origin),
                                    "axis": _vector(n, face_boss["sign"]),
                                    "ref": _vector(ua),
                                },
                                "profile": "circle",
                                "depth_mm": round(face_boss["depth"], 3),
                                "diameter_mm": diameter,
                            },
                            "confidence": 0.5,
                        }
                    )
                continue
            near_min = frame.near == "min"
            from_min = near_min if visible else not near_min
            start = 0.0 if from_min else extent[n]
            stop = extent[n] if from_min else 0.0
            reach = 0.0
            for other in frames.values():
                if other is frame or n not in (other.u[0], other.v[0]):
                    continue
                shared = other.u[0] if other.v[0] == n else other.v[0]
                walls = []
                for offset in (-radius_mm, radius_mm):
                    fixed = {shared: centre[shared] + offset}
                    walls.append(line_extent(ink, other, n, fixed, start, stop))
                reach = max(reach, min(walls))
            if r < 3 * line and reach < 2 * line * scale:
                continue  # мелкий круг без стенок на соседних видах — петля цифры
            through = reach >= 0.9 * extent[n] or reach < 2 * line * scale
            diameter = _match(2 * radius_mm, diameters, 0.06) or round(2 * radius_mm, 3)
            origin = dict(centre)
            origin[n] = start
            hole_params: dict[str, Any] = {
                "placement": {
                    "origin": _point(origin),
                    "axis": _vector(n, 1.0 if from_min else -1.0),
                    "ref": _vector(ua),
                },
                "diameter_mm": diameter,
                "through": through,
            }
            if not through:
                hole_params["depth_mm"] = round(reach, 3)
            if any(
                h["axis"] == n
                and abs(h["centre"][ua] - centre[ua]) < 1.0
                and abs(h["centre"][va] - centre[va]) < 1.0
                for h in holes
            ):
                continue
            holes.append({"axis": n, "centre": centre, "params": hole_params, "view": side})
    features.extend({"kind": "hole", "params": h["params"], "confidence": 0.5} for h in holes)
    # Номиналы: замер на листе точен до десятых, а деталь задана надписями —
    # размеры элементов, их удаление от граней и соосность с телом берутся
    # по надписям и симметрии, если замер к ним близок.
    _nominal_features(features, extent, linear)

    # Габарит с приливами — для сверки с эталоном и отчёта.
    mx, my, mw, mh = main.box
    overall = {
        "x": max([mw] + [views[k].box[2] for k in ("below", "above") if k in views]) * scale,
        "z": max([mh] + [views[k].box[3] for k in ("right", "left") if k in views]) * scale,
        "y": max(
            [views[k].box[3] for k in ("below", "above") if k in views]
            + [views[k].box[2] for k in ("right", "left") if k in views]
            or [depth / scale]
        )
        * scale,
    }
    candidate = {
        "candidate": {"features": features, "score": 0.6, "label": part or "деталь"},
        "confirm_assumptions": True,
        "metadata": {"source": "cad_views", "method": "prismatic"},
    }
    return ViewsResult(
        True,
        candidate=candidate,
        profile={
            "kind": "prismatic",
            "bounds_mm": [
                _match(overall[a], linear, 0.03) or round(overall[a], 3) for a in ("x", "y", "z")
            ],
            "body_mm": [width, depth, height],
            # Размеры элементов — тоже надписи листа: полость, приливы.
            "feature_lengths_mm": _feature_lengths(features, extent),
            "views": sorted(used),
            "polygons": polygons,
            "source_box": [mx, my, mx + mw, my + mh],
        },
        features=[
            *(
                {
                    "kind": "hole",
                    "diameter_mm": h["params"]["diameter_mm"],
                    "through": h["params"]["through"],
                    "view": h["view"],
                }
                for h in holes
            ),
            *(
                {"kind": "boss", "diameter_mm": boss["size"]}
                for boss in bosses
                if boss["profile"] == "circle"
            ),
        ],
        scales={
            "mm_per_px": scale,
            "labels_explained": len(what),
            "views_disagree": disagreement,
        },
        notes=[
            "виды: " + ", ".join(sorted(used)),
            *notes,
            "контур: " + "; ".join(tried),
        ],
    )


__all__ = ["arrange_views", "build_prismatic", "view_polygon"]
