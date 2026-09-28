"""Призматическая деталь по видам как инженер: тело, приливы, отверстия, полости.

Пересечение контуров видов («визуальная оболочка») не выражает того, что
видно только сопоставлением видов: прилив Ø16 на стенке — на одном виде
окружность, на другом выступ; отверстие — окружность на одном виде и пара
линий на глубину на другом; полость — пустота на разрезе. Здесь каждый вид
получает систему координат детали от кромок ТЕЛА (длинные основные линии, а
не рамка контура с приливами), и элементы собираются по совпадению на видах.

Координаты детали (как у `view_polygon`): X — ширина главного вида вправо,
Z — его высота вверх, Y — вглубь от наблюдателя главного вида. Раскладка
ЕСКД (первый угол): под главным — вид сверху, справа — вид слева, над ним —
вид снизу, слева — вид справа.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Оси листа каждого вида: (ось детали вправо, знак), (ось вверх, знак),
# ось взгляда и ближняя к наблюдателю грань ("min" | "max" по этой оси).
_AXES = {
    "main": (("x", 1), ("z", 1), "y", "min"),
    "below": (("x", 1), ("y", 1), "z", "max"),
    "above": (("x", 1), ("y", -1), "z", "min"),
    "right": (("y", -1), ("z", 1), "x", "min"),
    "left": (("y", 1), ("z", 1), "x", "max"),
}


@dataclass
class ViewFrame:
    side: str
    outline: Any
    rect: tuple[float, float, float, float]  # кромки тела на листе: L, T, R, B (px)
    scale: float  # мм/px
    section: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def u(self) -> tuple[str, int]:
        return _AXES[self.side][0]

    @property
    def v(self) -> tuple[str, int]:
        return _AXES[self.side][1]

    @property
    def normal(self) -> str:
        return _AXES[self.side][2]

    @property
    def near(self) -> str:
        return _AXES[self.side][3]

    def to_part(self, px: float, py: float) -> dict[str, float]:
        left, top, right, bottom = self.rect
        (ua, us), (va, vs) = self.u, self.v
        u = (px - left) if us > 0 else (right - px)
        v = (bottom - py) if vs > 0 else (py - top)
        return {ua: u * self.scale, va: v * self.scale}

    def to_sheet(self, point: dict[str, float]) -> tuple[float, float]:
        left, top, right, bottom = self.rect
        (ua, us), (va, vs) = self.u, self.v
        u, v = point[ua] / self.scale, point[va] / self.scale
        px = left + u if us > 0 else right - u
        py = bottom - v if vs > 0 else top + v
        return px, py

    def span(self, axis: str, a_px: float, b_px: float) -> tuple[float, float]:
        """Диапазон координаты детали по отрезку листа вдоль оси вида."""
        if axis == self.u[0]:
            one, two = (
                self.to_part(a_px, self.rect[1])[axis],
                self.to_part(b_px, self.rect[1])[axis],
            )
        else:
            one, two = (
                self.to_part(self.rect[0], a_px)[axis],
                self.to_part(self.rect[0], b_px)[axis],
            )
        return (min(one, two), max(one, two))


def line_masks(gray: Any) -> tuple[Any, Any, Any, float]:
    """(чернила, основные, тонкие, толщина основной) для всего листа."""
    import cv2
    import numpy as np

    from app.ai.cad_views.extrude_body import main_line_mask

    ink, thick, line = main_line_mask(gray)
    thin = ink & (1 - cv2.dilate(thick, np.ones((3, 3), np.uint8)))
    return ink, thick, thin, line


def _clusters(values: list[int], gap: int) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    for value in sorted(values):
        if groups and value - groups[-1][1] <= gap:
            groups[-1] = (groups[-1][0], value)
        else:
            groups.append((value, value))
    return groups


def _longest_run(row: Any) -> int:
    import numpy as np

    if not row.any():
        return 0
    edges = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
    return int((ends - starts).max())


def _long_runs(row: Any, minimum: float) -> int:
    """Сумма длин прогонов не короче ``minimum``."""
    import numpy as np

    if not row.any():
        return 0
    edges = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
    lengths = np.where(edges == -1)[0] - np.where(edges == 1)[0]
    return int(lengths[lengths >= minimum].sum())


def edge_lines(thick: Any, outline: Any) -> dict[str, list[tuple[float, float]]]:
    """Основные линии вида вдоль осей: {"rows": [(y, доля)], "cols": [(x, доля)]}.

    Доля — длина линии относительно самой длинной линии того же направления
    на виде (кромка тела — 1, кромка прилива — меньше)."""
    import cv2
    import numpy as np

    x, y, w, h = outline.box
    line = outline.line
    pad = int(2 * line) + 1
    height, width = thick.shape[:2]
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(width, x + w + pad), min(height, y + h + pad)
    crop = thick[y0:y1, x0:x1]
    # Основная линия прерывается там, где к ней примыкает другая, — пропуски
    # до двух толщин линии смыкаются.
    k = int(2 * line) | 1
    rows_mask = cv2.morphologyEx(crop, cv2.MORPH_CLOSE, np.ones((1, k), np.uint8))
    cols_mask = cv2.morphologyEx(crop, cv2.MORPH_CLOSE, np.ones((k, 1), np.uint8))
    gap = max(2, int(line))
    found: dict[str, list[tuple[float, float]]] = {}
    # Длина — сумма прямых кусков линии не короче 4 толщин: кромку тела рвут
    # приливы и стрелки (разрез корпуса 8: левая кромка — два куска по 0,4),
    # а короткие куски — штрихи штриховки поперёк строки.
    piece = 4 * line
    for name, runs, size, origin in (
        ("rows", [_long_runs(rows_mask[i], piece) for i in range(crop.shape[0])], w, y0),
        ("cols", [_long_runs(cols_mask[:, j], piece) for j in range(crop.shape[1])], h, x0),
    ):
        picked = [i for i, run in enumerate(runs) if run >= 0.12 * size]
        groups = _clusters(picked, gap)
        lengths = [max(runs[a : b + 1]) for a, b in groups]
        longest = max(lengths, default=1) or 1
        found[name] = [
            (origin + (a + b) / 2.0, length / longest) for (a, b), length in zip(groups, lengths)
        ]
    return found


def body_rect(
    thick: Any, outline: Any, lines: dict[str, list[tuple[float, float]]] | None = None
) -> tuple[float, float, float, float]:
    """Кромки тела на виде — крайние длинные основные линии.

    Рамка контура шире тела на приливы; кромка тела — линия не короче 0,85
    самой длинной (кромка прилива короче — у прилива Ø30 на стенке 40 доля 0,75; размерные — тонкие)."""
    x, y, w, h = outline.box
    lines = lines or edge_lines(thick, outline)

    def edges(items: list[tuple[float, float]]) -> list[float]:
        # Кромку тела рвут стрелки и цифры размеров у неё — если длинных
        # линий меньше двух, порог ниже (рамка контура — только в крайнем).
        for share_min in (0.85, 0.6):
            found = [c for c, share in items if share >= share_min]
            if len(found) >= 2:
                return found
        return []

    rows, cols = edges(lines["rows"]), edges(lines["cols"])
    top, bottom = (min(rows), max(rows)) if len(rows) >= 2 else (float(y), float(y + h))
    left, right = (min(cols), max(cols)) if len(cols) >= 2 else (float(x), float(x + w))
    return (left, top, right, bottom)


def _refine(
    thick: Any, item: dict[str, Any], rect: tuple[float, float, float, float], line: float
) -> dict[str, Any] | None:
    """Пролёт и вылет выступа — по его основным линиям, а не по заливке.

    Заливка контура захватывает стрелки и цифры размеров у выступа (вылет
    прилива 10 мм выходил 16); торец прилива — основная линия поперёк
    вылета, бока — вдоль."""
    left, top, right, bottom = rect
    side, a0, a1, depth = item["side"], item["a0"], item["a1"], item["depth"]
    # Вырез листа в системе «вдоль кромки × наружу от кромки».
    height, width = thick.shape[:2]
    pad = int(line) + 1
    if side in ("top", "bottom"):
        edge = bottom if side == "bottom" else top
        lo, hi = int(max(0, a0 - pad)), int(min(width, a1 + pad))
        if side == "bottom":
            out = thick[int(edge) : int(min(height, edge + depth + pad)), lo:hi]
        else:
            out = thick[int(max(0, edge - depth - pad)) : int(edge) + 1, lo:hi][::-1]
    else:
        edge = right if side == "right" else left
        lo, hi = int(max(0, a0 - pad)), int(min(height, a1 + pad))
        if side == "right":
            out = thick[lo:hi, int(edge) : int(min(width, edge + depth + pad))].T
        else:
            out = thick[lo:hi, int(max(0, edge - depth - pad)) : int(edge) + 1][:, ::-1].T
    if out.size == 0:
        return item
    span = a1 - a0
    rows = [i for i in range(out.shape[0]) if _longest_run(out[i]) >= 0.5 * span]
    rows = [i for i in rows if i > 1.5 * line]
    if not rows:
        return None  # торца нет — пятно стрелок и цифр у кромки, не прилив
    depth = float(sum(_clusters(rows, max(2, int(line)))[-1]) / 2.0)
    inner = out[int(line) : int(max(line + 1, depth))]
    if inner.shape[0] > 0:
        cols = [
            j for j in range(inner.shape[1]) if _longest_run(inner[:, j]) >= 0.5 * inner.shape[0]
        ]
        if len(cols) >= 2:
            groups = _clusters(cols, max(2, int(line)))
            first, last = groups[0], groups[-1]
            if len(groups) >= 2:
                a0, a1 = lo + sum(first) / 2.0, lo + sum(last) / 2.0
    if a1 - a0 < 3 * line:
        return None
    return {**item, "a0": float(a0), "a1": float(a1), "depth": float(depth), "c": (a0 + a1) / 2.0}


def protrusions(
    outline: Any, rect: tuple[float, float, float, float], thick: Any = None
) -> list[dict[str, Any]]:
    """Выступы контура за кромки тела: сторона, пролёт вдоль кромки, вылет (px)."""
    import cv2
    import numpy as np

    filled = outline.filled.astype(np.uint8).copy()
    fx, fy = outline.filled_origin
    line = outline.line
    left, top, right, bottom = (int(round(v)) for v in rect)
    margin = int(line)
    filled[
        max(0, top - fy - margin) : max(0, bottom - fy + margin + 1),
        max(0, left - fx - margin) : max(0, right - fx + margin + 1),
    ] = 0
    size = int(2 * line) | 1
    filled = cv2.morphologyEx(filled, cv2.MORPH_OPEN, np.ones((size, size), np.uint8))
    count, _labels, stats, _ = cv2.connectedComponentsWithStats(filled, 8)
    found: list[dict[str, Any]] = []
    for index in range(1, count):
        bx, by, bw, bh, area = (int(v) for v in stats[index])
        if area < (3 * line) ** 2 or min(bw, bh) < 2 * line:
            continue
        bx, by = bx + fx, by + fy
        cx, cy = bx + bw / 2.0, by + bh / 2.0
        # Сторона — та, за которую выступ ушёл дальше всего.
        outs = {
            "left": left - bx,
            "right": bx + bw - right,
            "top": top - by,
            "bottom": by + bh - bottom,
        }
        side = max(outs, key=lambda k: outs[k])
        if outs[side] < 1.5 * line:
            continue
        if side in ("left", "right"):
            span = (float(by), float(by + bh), cy)
        else:
            span = (float(bx), float(bx + bw), cx)
        item = {
            "side": side,
            "a0": span[0],
            "a1": span[1],
            "depth": float(outs[side]),
            "c": span[2],
        }
        refined = _refine(thick, item, rect, line) if thick is not None else item
        if refined is not None:
            found.append(refined)
    return found


def ring_cover(mask: Any, cx: float, cy: float, r: float, line: float) -> float:
    """Доля окружности (cx, cy, r) на листе, покрытая маской."""
    import numpy as np

    angles = np.radians(np.arange(0, 360, 4))
    band = np.arange(-max(1.0, 0.6 * line), max(1.0, 0.6 * line) + 0.5, 1.0)
    height, width = mask.shape[:2]
    hits = 0
    for angle in angles:
        xs = np.clip((cx + (r + band) * np.cos(angle)).astype(int), 0, width - 1)
        ys = np.clip((cy + (r + band) * np.sin(angle)).astype(int), 0, height - 1)
        hits += bool(mask[ys, xs].any())
    return hits / len(angles)


def line_extent(
    mask: Any, frame: ViewFrame, along: str, fixed: dict[str, float], start: float, stop: float
) -> float:
    """Докуда от ``start`` к ``stop`` (мм по оси ``along``) идёт линия.

    Линия — невидимая (штрихи) или видимая: окно в 2 толщины линии считается
    занятым, если в нём есть чернила; обрыв — два пустых окна подряд."""
    line = frame.outline.line
    step = max(frame.scale, 0.5 * line * frame.scale)
    direction = 1.0 if stop >= start else -1.0
    reach = start
    empty = 0
    position = start
    height, width = mask.shape[:2]
    window_mm = 2.0 * line * frame.scale
    while (position - stop) * direction <= 0:
        point = dict(fixed)
        point[along] = position
        px, py = frame.to_sheet(point)
        r = int(max(1, 0.7 * line))
        x0, x1 = int(px) - r, int(px) + r + 1
        y0, y1 = int(py) - r, int(py) + r + 1
        if 0 <= x0 and x1 <= width and 0 <= y0 and y1 <= height and mask[y0:y1, x0:x1].any():
            reach = position
            empty = 0
        else:
            empty += step
            if empty > max(window_mm, 3 * line * frame.scale) * 1.5:
                break
        position += direction * step
    return abs(reach - start)


def _segment_samples(
    mask: Any, frame: ViewFrame, fixed: dict[str, float], along: str, low: float, high: float
) -> list[bool]:
    """Есть ли чернила в точках отрезка (ось ``along`` от low до high при ``fixed``)."""
    line = frame.outline.line
    step = max(frame.scale, 0.5 * line * frame.scale)
    height, width = mask.shape[:2]
    r = int(max(1, 0.7 * line))
    samples: list[bool] = []
    # Окно — поперёк линии (линия может лечь на пиксель в сторону), вдоль —
    # в один пиксель: разрыв штриховой в полтора миллиметра иначе тонет.
    a = dict(fixed)
    a[along] = low
    b = dict(fixed)
    b[along] = high
    (ax, ay), (bx, by) = frame.to_sheet(a), frame.to_sheet(b)
    rx, ry = (0, r) if abs(bx - ax) >= abs(by - ay) else (r, 0)
    position = low
    while position <= high:
        point = dict(fixed)
        point[along] = position
        px, py = frame.to_sheet(point)
        x0, y0 = int(px) - rx, int(py) - ry
        inside = 0 <= x0 and x0 + 2 * rx < width and 0 <= y0 and y0 + 2 * ry < height
        samples.append(bool(inside and mask[y0 : y0 + 2 * ry + 1, x0 : x0 + 2 * rx + 1].any()))
        position += step
    return samples


def _segment_cover(
    mask: Any, frame: ViewFrame, fixed: dict[str, float], along: str, low: float, high: float
) -> float:
    """Доля отрезка под чернилами."""
    samples = _segment_samples(mask, frame, fixed, along, low, high)
    return sum(samples) / len(samples) if samples else 0.0


def _dashed(samples: list[bool]) -> bool:
    """Штриховая: покрыта в основном, но с несколькими разрывами внутри.

    Сплошная тонкая (выносная, размерная) рвётся разве что на подписи —
    один-два разрыва; штриховая — через каждые несколько миллиметров."""
    if len(samples) < 6 or sum(samples) < 0.5 * len(samples):
        return False
    gaps = 0
    for before, now in zip(samples, samples[1:]):
        gaps += before and not now
    if not samples[-1]:
        gaps -= 1
    return gaps >= (3 if len(samples) >= 30 else 2)


def cavity_span(
    thin: Any,
    frame: ViewFrame,
    n: str,
    shared: str,
    v0: float,
    v1: float,
    size: float,
    shared_size: float | None = None,
) -> tuple[float, float, float, float] | None:
    """Пределы полости по оси ``n`` на виде, где она видна невидимым контуром.

    Пролёт по общей оси ``shared`` известен с разреза; на этом виде полость —
    штриховой прямоугольник: две линии поперёк и две стенки вдоль ``n`` на
    концах пролёта. Замкнутая фигура не нужна (её режут выносные и размерные);
    штриховая отличается от сплошной тонкой (выносной, размерной) разрывами:
    покрыта чернилами не сплошь.

    Возвращает (начало, конец по ``n``, начало, конец по ``shared``): пролёт
    по общей оси уточняется концами штриховых — пустоту на разрезе режут
    размеры, и она бывает уже полости (корпус 8: 32 вместо 14,6)."""
    line = frame.outline.line * frame.scale
    inset = min(2 * line, 0.2 * (v1 - v0))
    step = 0.5 * line
    dashed_rows: list[int] = []
    solid_rows: list[int] = []
    for k in range(int(size / step) + 1):
        samples = _segment_samples(thin, frame, {n: k * step}, shared, v0 + inset, v1 - inset)
        if _dashed(samples):
            dashed_rows.append(k)
        elif samples and sum(samples) >= 0.7 * len(samples):
            solid_rows.append(k)

    def centres(rows: list[int]) -> set[float]:
        return {round((a + b) / 2.0 * step, 3) for a, b in _clusters(rows, 2)}

    dashed = centres(dashed_rows)
    # Вторая кромка может совпасть со сплошной тонкой (выносной размера
    # глубины) или с гранью тела — полость открыта на неё (корпус 4: верхняя
    # штриховая находилась, нижняя шла по выносной «17»).
    drawn = centres(solid_rows)
    faces = {0.0, round(size, 3)}
    positions = sorted(dashed | drawn | faces)
    best: tuple[int, int, float, float, float, float, float, float] | None = None
    for i, a in enumerate(positions):
        for b in positions[i + 1 :]:
            if b - a < 2 * line:
                continue
            count = int(a in dashed) + int(b in dashed)
            if count == 0:
                continue
            # Концы поперечных — там стенки (штриховая идёт от стенки до
            # стенки); без них — края пролёта с разреза.
            mid = (v0 + v1) / 2.0
            lows, highs = [v0], [v1]
            if shared_size:
                for row in (a, b):
                    if row in faces:
                        continue
                    lows.append(mid - line_extent(thin, frame, shared, {n: row}, mid, 0.0))
                    highs.append(mid + line_extent(thin, frame, shared, {n: row}, mid, shared_size))
            walls = []
            for options_at in (sorted(set(lows)), sorted(set(highs))):
                tried = []
                for wall in options_at:
                    for k in (-1, -0.5, 0, 0.5, 1):
                        samples = _segment_samples(
                            thin, frame, {shared: wall + k * line}, n, a + line, b - line
                        )
                        cover = sum(samples) / len(samples) if samples else 0.0
                        tried.append((cover, wall + k * line))
                walls.append(max(tried))
            # Стенки часто совпадают со сплошными выносными размеров полости —
            # от них нужна только непрерывность, штриховая — поперечные линии.
            cover = min(walls[0][0], walls[1][0])
            if cover < 0.6:
                continue
            # Линия на конце весомее грани тела: по выносным до грани стенки
            # «тянутся» всегда (корпус 4: 38,9…60 вместо 21,4…38,9).
            lines_at = int(a not in faces) + int(b not in faces)
            key = (count, lines_at, round(cover, 1), b - a)
            if best is None or key > best[:4]:
                best = (*key, a, b, walls[0][1], walls[1][1])
    return (best[4], best[5], best[6], best[7]) if best else None


def visible_rectangles(thick: Any, frame: ViewFrame) -> list[tuple[float, float, float, float]]:
    """Замкнутые прямоугольники основной линии внутри тела вида (px листа).

    Карман на грани, обращённой к наблюдателю, виден прямоугольником
    основной линии; окружности (отверстия) заполняют свою рамку на π/4 и
    сюда не попадают."""
    import cv2
    import numpy as np

    left, top, right, bottom = (int(round(v)) for v in frame.rect)
    line = frame.outline.line
    crop = thick[top : bottom + 1, left : right + 1]
    if crop.size == 0:
        return []
    closed = cv2.morphologyEx(crop, cv2.MORPH_CLOSE, np.ones((int(2 * line) | 1,) * 2, np.uint8))
    contours, hierarchy = cv2.findContours(closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    found = []
    minimum = 3.0 / frame.scale  # мм → px
    for index, contour in enumerate(contours):
        if hierarchy is None or hierarchy[0][index][3] == -1:
            continue  # внутренняя граница — дыра в линиях
        x, y, w, h = cv2.boundingRect(contour)
        if w < minimum or h < minimum:
            continue
        if w > 0.9 * crop.shape[1] and h > 0.9 * crop.shape[0]:
            continue  # само тело вида
        if cv2.contourArea(contour) < 0.85 * w * h:
            continue
        # До середины линии: дыра в линиях — внутренний край.
        pad = line / 2.0
        found.append((left + x - pad, top + y - pad, w + 2 * pad, h + 2 * pad))
    return found


__all__ = [
    "visible_rectangles",
    "ViewFrame",
    "body_rect",
    "cavity_span",
    "edge_lines",
    "line_extent",
    "line_masks",
    "protrusions",
    "ring_cover",
]
