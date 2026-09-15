"""Контур пластины по листу: стороны и сопряжения замером, числа — надписями (Ф4/X1).

Живая планка part_04 (Г-образная, s3): ридер прочитал её прямоугольником
90 × 100 с двумя отверстиями не на местах, третье пропустил; не прямоугольный
контур ридер не выражает — тела не было. Как у профиля вала по листу
(`sheet_profile`): контур главного вида — замером, точные числа — надписями,
которые ридер выписал сам.

* Контур детали — замкнутый контур основной линией, у которого отношение
  сторон рамки совпадает с отношением двух надписей габарита (0,899 при
  90 × 100); рамка листа, штамп и промежутки размеров так не совпадают.
* Стороны вдоль осей (первая версия — только они): горизонтальные и
  вертикальные участки контура, координата — надписью: 0, габарит, надпись
  или габарит минус надпись.
* Угол между соседними сторонами — острый или сопряжение: контур срезает угол
  на R(√2 − 1), радиус — ближайшая надпись R.
* Отверстия — окружности с радиусом надписи Ø внутри контура; центры —
  надписями от кромок или по цепочке от соседнего отверстия (планка: 80 =
  90 − 10, 16 = 80 − 64, 90 = 14 + 76).

Любая сторона, угол или отверстие, не объяснённые надписью однозначно, —
отказ целиком. Выход — ``profile.shape = "sketch"``: первая вершина эскиза —
левый нижний угол (0, 0), отверстия — от неё же.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_RADIUS = re.compile(r"(?<![A-Za-zА-Яа-я])R\s*(\d+(?:[.,]\d+)?)")
_THICKNESS = re.compile(r"(?<![A-Za-zА-Яа-я])[sSЅ]\s*(\d+(?:[.,]\d+)?)")
# Отношение сторон рамки контура к отношению надписей габарита.
_RATIO_TOLERANCE = 0.02
# Сторона — прямой участок не короче этого (мм); короче — зубец выноски или
# осевой, касающейся контура.
_MIN_SIDE_MM = 2.5
# Направление участка: отклонение от оси не больше этого (градусы).
_AXIS_DEG = 12.0
# Угол срезан больше этого — сопряжение, а не острый угол (мм).
_FILLET_MIN_MM = 1.2
# Радиус сопряжения — надпись R в пределах этой доли.
_RADIUS_SHARE = 0.25
_AMBIGUOUS = 0.25
_DIAMETER_SHARE = 0.10


@dataclass(frozen=True)
class ContourProposal:
    """Пластина, собранная по листу: профиль для спека и точность листа."""

    profile: dict[str, Any]
    side_error_mm: float
    bbox_px: tuple[float, float, float, float]
    notes: tuple[str, ...] = field(default=())


def plate_labels(spec: dict[str, Any]) -> dict[str, list[float]]:
    """Надписи пластины: осевые (голые числа), Ø, R, толщина."""
    from app.ai.cad_recognize.verifiers.sheet_profile import sheet_labels

    base = sheet_labels(spec)
    radii: set[float] = set()
    thickness: set[float] = set()
    for item in spec.get("dimensions") or []:
        text = str((item.get("value") if isinstance(item, dict) else item) or "").strip()
        # Только короткие надписи: пояснения ридера несут чужие числа.
        if len(text) > 12:
            continue
        for match in _RADIUS.finditer(text):
            radii.add(float(match.group(1).replace(",", ".")))
        for match in _THICKNESS.finditer(text):
            thickness.add(float(match.group(1).replace(",", ".")))
    return {
        "axial": [value for value, _column in base.axial],
        "diameters": list(base.diameters),
        "radii": sorted(radii),
        "thickness": sorted(thickness),
    }


def propose_contour(
    gray: Any, spec: dict[str, Any], *, read_holes: int = 0
) -> tuple[ContourProposal | None, str]:
    """Контур пластины по листу или ``None`` с причиной."""
    import cv2
    import numpy as np

    from app.ai.cad_recognize.sheet_upscale import main_line_px
    from app.ai.cad_recognize.verifiers.plate_frame import _ink
    from app.ai.cad_recognize.verifiers.shaft_profile import _MIN_LINE_PX

    gray = np.asarray(gray)
    labels = plate_labels(spec)
    axial = sorted({v for v in labels["axial"] if v > 0})
    counts = Counter(v for v in labels["axial"] if v > 0)
    if len(axial) < 2:
        return None, "на листе нет надписей габарита"
    line_px = main_line_px(gray)
    if line_px < _MIN_LINE_PX:
        return None, f"лист слишком грубый: основная линия {line_px:.1f} px"
    ink = _ink(gray).astype(np.uint8)
    kernel = max(3, int(round(0.7 * line_px)))
    thick = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((kernel, kernel), np.uint8))
    contours, hierarchy = cv2.findContours(thick, cv2.RETR_TREE, cv2.CHAIN_APPROX_NONE)
    if hierarchy is None:
        return None, "замкнутых контуров нет"
    height_px, width_px = gray.shape
    candidates = []
    for index, contour in enumerate(contours):
        if hierarchy[0][index][3] < 0:
            continue  # внешняя граница штриха; нужен контур области
        area = cv2.contourArea(contour)
        if not 0.005 * height_px * width_px <= area <= 0.4 * height_px * width_px:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        # Одна надпись не бывает и шириной, и высотой (живая планка: область
        # вместе с размерами «совпадала» с 100 × 100 при одной «100»).
        pairs = [
            (big_w, big_h)
            for big_w in axial
            for big_h in axial
            if (big_w != big_h or counts[big_w] > 1)
            and abs((w / h) / (big_w / big_h) - 1.0) <= _RATIO_TOLERANCE
        ]
        if pairs:
            candidates.append((area, index, (x, y, w, h), pairs))
    if not candidates:
        return None, "контура с отношением сторон, как у надписей габарита, нет"
    # Габарит — обычно самая большая надпись листа: вперёд контур, чья пара её
    # содержит, затем больший по площади.
    largest = max(axial)
    candidates.sort(key=lambda item: (not any(largest in pair for pair in item[3]), -item[0]))
    _area, index, (x0, y0, w, h), pairs = candidates[0]
    # Габарит — самые большие надписи с таким отношением (планка: 90 × 100, а
    # не 9 × 10).
    width, height = max(pairs, key=lambda pair: pair[0] * pair[1])
    sx, sy = width / w, height / h
    points = contours[index][:, 0, :].astype(float)
    uv = np.c_[(points[:, 0] - x0) * sx, (y0 + h - points[:, 1]) * sy]
    sides = _sides(uv)
    if len(sides) < 4:
        return None, "у контура меньше четырёх сторон вдоль осей"
    if any(a["kind"] == b["kind"] for a, b in zip(sides, sides[1:] + sides[:1])):
        return None, "контур не только из сторон вдоль осей (наклон или ступенька)"
    snapped, side_error = _snap_sides(sides, axial, width, height)
    if isinstance(snapped, str):
        return None, snapped
    corners = _corners(snapped, uv, labels["radii"])
    if isinstance(corners, str):
        return None, corners
    sketch, origin = _sketch(snapped, corners)
    holes = _holes(gray, ink, contours[index], (x0, y0, w, h), sx, sy, labels, axial, width, height)
    if isinstance(holes, str):
        return None, holes
    if len(holes) < read_holes:
        return None, f"отверстий на листе найдено {len(holes)}, прочитано {read_holes}"
    thickness = labels["thickness"][0] if len(labels["thickness"]) == 1 else None
    profile = {
        "shape": "sketch",
        "width_mm": width,
        "height_mm": height,
        "thickness_mm": thickness,
        "sketch": sketch,
        "holes": [
            {
                "center_x_mm": round(hx - origin[0], 3),
                "center_y_mm": round(hy - origin[1], 3),
                "diameter_mm": d,
            }
            for d, hx, hy in holes
        ],
    }
    return ContourProposal(
        profile=profile, side_error_mm=side_error, bbox_px=(x0, y0, x0 + w, y0 + h)
    ), ""


def _sides(uv: Any) -> list[dict[str, Any]]:
    """Прямые участки вдоль осей по порядку обхода: вид, координата, направление."""
    import numpy as np

    n = len(uv)
    step = float(np.median(np.linalg.norm(np.diff(uv, axis=0), axis=1))) or 0.1
    half = max(3, int(round(0.8 / step)))  # окно ~1,6 мм
    kinds = []
    for i in range(n):
        d = uv[(i + half) % n] - uv[(i - half) % n]
        angle = math.degrees(math.atan2(abs(d[1]), abs(d[0])))
        kinds.append("H" if angle <= _AXIS_DEG else "V" if angle >= 90 - _AXIS_DEG else "C")
    # Прогоны одного вида по кругу, начиная с границы прогонов.
    start = next((i for i in range(n) if kinds[i] != kinds[i - 1]), 0)
    runs: list[list[int]] = []
    for offset in range(n):
        i = (start + offset) % n
        if runs and kinds[runs[-1][-1]] == kinds[i]:
            runs[-1].append(i)
        else:
            runs.append([i])
    sides = []
    for run in runs:
        kind = kinds[run[0]]
        if kind == "C":
            continue
        pts = uv[run]
        axis = 1 if kind == "H" else 0
        other = 0 if kind == "H" else 1
        # Длина — сдвиг от начала к концу вдоль стороны: осевая отверстия,
        # пересекающая кромку, — зубец туда и обратно (живая планка: уходит
        # внутрь на 3–4 мм, а сдвиг 0,2 мм).
        length = abs(float(pts[-1, other] - pts[0, other]))
        if length < _MIN_SIDE_MM:
            continue
        side = {
            "kind": kind,
            "value": float(np.median(pts[:, axis])),
            "direction": 1.0 if pts[-1, other] > pts[0, other] else -1.0,
            "first": int(run[0]),
            "last": int(run[-1]),
        }
        # Зубец разрывает сторону на две того же вида и уровня — сшить.
        if sides and sides[-1]["kind"] == kind and abs(sides[-1]["value"] - side["value"]) < 0.8:
            sides[-1]["last"] = side["last"]
            continue
        sides.append(side)
    if (
        len(sides) > 1
        and sides[0]["kind"] == sides[-1]["kind"]
        and abs(sides[0]["value"] - sides[-1]["value"]) < 0.8
    ):
        sides[0]["first"] = sides[-1]["first"]
        sides.pop()
    return sides


def _snap_sides(
    sides: list[dict[str, Any]], axial: list[float], width: float, height: float
) -> tuple[list[dict[str, Any]], float] | tuple[str, float]:
    """Координата каждой стороны — надписью: 0, габарит, надпись, габарит − надпись."""
    worst = 0.0
    snapped = []
    for side in sides:
        extent = width if side["kind"] == "V" else height
        options = {0.0, extent}
        for label in axial:
            if 0 < label < extent:
                options.update({label, round(extent - label, 3)})
        tolerance = max(0.6, 0.02 * extent)
        ranked = sorted(options, key=lambda value: abs(value - side["value"]))
        best = ranked[0]
        residual = abs(best - side["value"])
        if residual > tolerance:
            return (
                f"сторона {'x' if side['kind'] == 'V' else 'y'} = {side['value']:.1f} мм "
                "не объясняется надписью",
                worst,
            )
        if (
            len(ranked) > 1
            and abs(ranked[1] - side["value"]) - residual < _AMBIGUOUS * tolerance
            and abs(ranked[1] - best) > 1e-6
        ):
            return (
                f"сторона {side['value']:.1f} мм: надписи {best:g} и {ranked[1]:g} почти одинаково",
                worst,
            )
        worst = max(worst, residual)
        snapped.append({**side, "value": best})
    return snapped, worst


def _corners(sides: list[dict[str, Any]], uv: Any, radii: list[float]) -> list[float] | str:
    """Угол между соседними сторонами: 0 — острый, иначе радиус по надписи R."""
    import numpy as np

    result = []
    n = len(uv)
    for side, nxt in zip(sides, sides[1:] + sides[:1]):
        corner = (
            np.array([side["value"], nxt["value"]])
            if side["kind"] == "V"
            else np.array([nxt["value"], side["value"]])
        )
        # Точки перехода — от конца стороны до начала следующей.
        i, j = side["last"], nxt["first"]
        count = (j - i) % n
        span = uv[[(i + k) % n for k in range(count + 1)]] if count else uv[[i]]
        cut = float(np.min(np.linalg.norm(span - corner, axis=1)))
        if cut < _FILLET_MIN_MM:
            result.append(0.0)
            continue
        estimate = cut / (math.sqrt(2.0) - 1.0)
        ranked = sorted(radii, key=lambda r: abs(r - estimate))
        if not ranked or abs(ranked[0] - estimate) > _RADIUS_SHARE * ranked[0]:
            return f"сопряжение R≈{estimate:.1f} мм у угла ({corner[0]:g}; {corner[1]:g}) не объясняется надписью R"
        if (
            len(ranked) > 1
            and abs(abs(ranked[1] - estimate) - abs(ranked[0] - estimate))
            < _AMBIGUOUS * _RADIUS_SHARE * ranked[0]
        ):
            return f"сопряжение R≈{estimate:.1f}: надписи R{ranked[0]:g} и R{ranked[1]:g} почти одинаково"
        result.append(ranked[0])
    return result


def _sketch(
    sides: list[dict[str, Any]], radii: list[float]
) -> tuple[list[dict[str, Any]], tuple[float, float]]:
    """Эскиз от левого нижнего угла против часовой стрелки; начало отсчёта."""
    corners = []
    for side, nxt, radius in zip(sides, sides[1:] + sides[:1], radii):
        point = (
            (side["value"], nxt["value"]) if side["kind"] == "V" else (nxt["value"], side["value"])
        )
        d1 = (0.0, side["direction"]) if side["kind"] == "V" else (side["direction"], 0.0)
        d2 = (0.0, nxt["direction"]) if nxt["kind"] == "V" else (nxt["direction"], 0.0)
        corners.append((point, d1, d2, radius))
    # Против часовой стрелки: знак площади многоугольника углов.
    area = sum(
        a[0][0] * b[0][1] - b[0][0] * a[0][1] for a, b in zip(corners, corners[1:] + corners[:1])
    )
    if area < 0:
        corners = [
            (point, (-d2[0], -d2[1]), (-d1[0], -d1[1]), radius)
            for point, d1, d2, radius in reversed(corners)
        ]
    # Каждый угол даёт вершину (острый) или касание — дугу — касание.
    path: list[tuple[str, tuple[float, float], tuple[float, float] | None, bool | None]] = []
    for point, d1, d2, radius in corners:
        if radius <= 0:
            path.append(("vertex", point, None, None))
            continue
        a = (point[0] - radius * d1[0], point[1] - radius * d1[1])
        b = (point[0] + radius * d2[0], point[1] + radius * d2[1])
        centre = (a[0] + radius * d2[0], a[1] + radius * d2[1])
        clockwise = d1[0] * d2[1] - d1[1] * d2[0] < 0
        path.append(("vertex", a, None, None))
        path.append(("arc", b, centre, clockwise))
    # Начало — левый нижний из вершин пути.
    starts = [k for k, item in enumerate(path) if item[0] == "vertex"]
    first = min(starts, key=lambda k: (round(path[k][1][0] + path[k][1][1], 6), path[k][1][0]))
    ordered = path[first:] + path[:first]
    origin = ordered[0][1]
    sketch = []
    for kind, point, centre, clockwise in ordered[1:] + ordered[:1]:
        to = (round(point[0] - origin[0], 3), round(point[1] - origin[1], 3))
        if kind == "arc":
            sketch.append(
                {
                    "kind": "arc",
                    "to": to,
                    "center": (round(centre[0] - origin[0], 3), round(centre[1] - origin[1], 3)),
                    "clockwise": clockwise,
                }
            )
        elif to != (sketch[-1]["to"] if sketch else (0.0, 0.0)):
            sketch.append({"kind": "line", "to": to})
    if sketch and sketch[-1]["to"] != (0.0, 0.0):
        sketch.append({"kind": "line", "to": (0.0, 0.0)})
    return _without_collinear(sketch), origin


def _without_collinear(sketch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Убрать вершину посреди прямой: сторона, разорванная зубцом, — одна линия."""
    result: list[dict[str, Any]] = []
    for segment in sketch:
        if segment["kind"] == "line" and result and result[-1]["kind"] == "line":
            before = result[-2]["to"] if len(result) > 1 else (0.0, 0.0)
            middle = result[-1]["to"]
            after = segment["to"]
            cross = (middle[0] - before[0]) * (after[1] - before[1]) - (middle[1] - before[1]) * (
                after[0] - before[0]
            )
            if abs(cross) < 1e-6:
                result[-1] = segment
                continue
        result.append(segment)
    return result


def _holes(
    gray: Any,
    ink: Any,
    contour: Any,
    box: tuple[int, int, int, int],
    sx: float,
    sy: float,
    labels: dict[str, list[float]],
    axial: list[float],
    width: float,
    height: float,
) -> list[tuple[float, float, float]] | str:
    """Отверстия: окружности радиуса надписи Ø внутри контура, центры — надписями."""
    import cv2
    import numpy as np

    diameters = labels["diameters"]
    if not diameters:
        return []
    x0, y0, w, h = box
    scale = (sx + sy) / 2.0
    r_min = int(0.8 * min(diameters) / 2.0 / scale)
    r_max = int(1.2 * max(diameters) / 2.0 / scale) + 1
    roi = cv2.medianBlur(np.ascontiguousarray(gray[y0 : y0 + h, x0 : x0 + w]), 5)
    circles = cv2.HoughCircles(
        roi,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=max(4, 2 * r_min),
        param1=120,
        param2=30,
        minRadius=max(3, r_min),
        maxRadius=r_max,
    )
    if circles is None:
        return []
    angles = np.linspace(0.0, 2.0 * np.pi, 360, endpoint=False)
    wide = cv2.dilate(ink, np.ones((5, 5), np.uint8)).astype(bool)
    found = []
    for cx, cy, r in circles[0]:
        gx, gy = x0 + float(cx), y0 + float(cy)
        if cv2.pointPolygonTest(contour, (gx, gy), False) <= 0:
            continue
        xs = np.clip(np.round(gx + r * np.cos(angles)).astype(int), 0, ink.shape[1] - 1)
        ys = np.clip(np.round(gy + r * np.sin(angles)).astype(int), 0, ink.shape[0] - 1)
        if float(wide[ys, xs].mean()) < 0.7:
            continue
        u, v = (gx - x0) * sx, (y0 + h - gy) * sy
        if any(math.hypot(u - a, v - b) < 0.5 * min(diameters) for _d, a, b in found):
            continue
        d = 2.0 * float(r) * scale
        ranked = sorted(diameters, key=lambda value: abs(value - d))
        if abs(ranked[0] - d) > _DIAMETER_SHARE * ranked[0]:
            continue
        found.append((ranked[0], u, v))
    return _snap_holes(found, axial, width, height)


def _snap_holes(
    holes: list[tuple[float, float, float]], axial: list[float], width: float, height: float
) -> list[tuple[float, float, float]] | str:
    """Центры — надписями от кромок или по цепочке от уже найденного отверстия."""
    known_x: list[float] = [0.0, width]
    known_y: list[float] = [0.0, height]
    result: list[tuple[float, float, float] | None] = [None] * len(holes)
    pending = list(range(len(holes)))
    while pending:
        best = None
        for index in pending:
            d, u, v = holes[index]
            fits = []
            for value, anchors, extent in ((u, known_x, width), (v, known_y, height)):
                tolerance = max(0.6, 0.02 * extent)
                options = {a + s * label for a in anchors for label in axial for s in (1, -1)}
                options = [o for o in options if 0 < o < extent]
                ranked = sorted(options, key=lambda o: abs(o - value))
                if not ranked or abs(ranked[0] - value) > tolerance:
                    fits = None
                    break
                fits.append((abs(ranked[0] - value), ranked[0]))
            if fits is None:
                continue
            residual = max(item[0] for item in fits)
            if best is None or residual < best[0]:
                best = (residual, index, fits[0][1], fits[1][1])
        if best is None:
            return f"центр отверстия не объясняется надписями ({len(pending)} из {len(holes)})"
        _residual, index, x, y = best
        result[index] = (holes[index][0], x, y)
        known_x.append(x)
        known_y.append(y)
        pending.remove(index)
    return [item for item in result if item is not None]
