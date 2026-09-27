"""Этап D2: деталь выдавливанием — контур вида в плане + толщина.

Как инженер: контур детали — замкнутая ОСНОВНАЯ линия; размерные и выносные
тонкие, граница «основная/тонкая» — по распределению толщин самого листа
(на растре из САПР тонкие 4 px при основных 7, на скане — вдвое тоньше).
Разметка областей листа дробит вид размерами (Г-образная планка — пять
областей), поэтому контур ищется по всему листу и лишь сверяется с
областями видов. Масштаб — пара надписей, объясняющая ширину и высоту
контура, и другие надписи (Ø отверстий, их положения); толщина — надпись
«sN» или второй вид в проекционной связи.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Outline:
    points: list[tuple[float, float]]  # px листа, обход контура
    box: tuple[int, int, int, int]  # x, y, w, h
    holes: list[tuple[float, float, float]] = field(default_factory=list)  # cx, cy, r px
    line: float = 3.0
    ink: Any = None  # чернила выреза вокруг контура
    ink_origin: tuple[int, int] = (0, 0)
    filled: Any = None  # залитая фигура контура
    filled_origin: tuple[int, int] = (0, 0)


def main_line_mask(gray: Any) -> tuple[Any, Any, float]:
    """(чернила, основные линии, толщина основной) — граница по гистограмме."""
    import cv2
    import numpy as np

    from app.ai.cad_views.pipeline import _line_px
    from app.ai.cad_views.section_material import ink_mask

    line = _line_px(gray)
    ink = ink_mask(gray, line)
    runs: list[int] = []
    for rows in (ink[:, ::3].T, ink[::3, :]):
        for row in rows:
            edges = np.diff(np.concatenate([[0], row, [0]]))
            runs.extend((np.where(edges == -1)[0] - np.where(edges == 1)[0]).tolist())
    histogram = np.bincount(np.clip(runs, 0, 60), minlength=61)
    upper = max(2, int(0.8 * line))
    thin = int(np.argmax(histogram[1 : upper + 1])) + 1
    kernel = max(thin + 1, int(round((thin + line) / 2.0)))
    thick = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel, kernel))
    )
    return ink, thick, line


def outlines(gray: Any, view_boxes: list[tuple[int, int, int, int]]) -> list[Outline]:
    """Замкнутые контуры основных линий, пересекающиеся с областями видов."""
    import cv2
    import numpy as np

    from app.ai.cad_views.view_features import _circles

    ink, thick, line = main_line_mask(gray)
    height, width = gray.shape[:2]
    # На дугах штрих основной линии местами тоньше границы — контур рвётся,
    # и «залитой» фигурой оказывался один штрих. Чернила вплотную к основным
    # линиям — их продолжение; просветы смыкаются шагом, пока контур не
    # станет замкнутым (площадь внутри много больше площади штриха).
    near = cv2.dilate(thick, np.ones((int(line) | 1, int(line) | 1), np.uint8))
    lines = thick | (ink & near)
    closed_shapes: list[Any] = []
    for step in (2, 3, 4, 6, 8, 10):
        size = int(step * line) | 1
        closed = cv2.morphologyEx(
            lines, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        )
        contours, hierarchy = cv2.findContours(closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
        for index, contour in enumerate(contours):
            if hierarchy[0][index][3] != -1:
                continue
            area = cv2.contourArea(contour)
            if area < 0.002 * width * height or area <= 4.0 * cv2.arcLength(contour, True) * line:
                continue  # мелочь или незамкнутый контур — один штрих
            box = cv2.boundingRect(contour)
            # Та же фигура на большем шаге — уже есть с меньшим.
            if any(_iou(box, cv2.boundingRect(other)) > 0.8 for other in closed_shapes):
                continue
            closed_shapes.append(contour)
    found: list[Outline] = []
    for contour in closed_shapes:
        x, y, w, h = cv2.boundingRect(contour)
        if w * h > 0.5 * width * height:
            continue
        overlaps = any(
            not (bx1 < x or bx0 > x + w or by1 < y or by0 > y + h)
            for bx0, by0, bx1, by1 in view_boxes
        )
        if not overlaps:
            continue
        # Залитая фигура без наконечников выносок и стрелок, прилипших к
        # контуру: размыкание кругом в пару толщин линии.
        filled = np.zeros((h + 2, w + 2), np.uint8)
        cv2.drawContours(filled, [contour - [x - 1, y - 1]], -1, 1, cv2.FILLED)
        opening = int(3 * line) | 1
        filled = cv2.morphologyEx(
            filled,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (opening, opening)),
        )
        # Контур — по середине основной линии, а не по её внешнему краю
        # (иначе габарит завышен на толщину линии, масштаб — занижен).
        half = max(1, int(round(line / 2.0)))
        centre = cv2.erode(
            filled, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * half + 1,) * 2)
        )
        inner, _ = cv2.findContours(centre, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not inner:
            continue
        shape = max(inner, key=cv2.contourArea)
        polygon = cv2.approxPolyDP(shape, 0.5 * line, True).reshape(-1, 2)
        points = [(float(px + x - 1), float(py + y - 1)) for px, py in polygon]
        sx0, sy0, sw, sh = cv2.boundingRect(shape)
        centre_box = (sx0 + x - 1, sy0 + y - 1, sw, sh)
        # Отверстия — окружности внутри контура, не касающиеся его.
        pad = int(line)
        crop_ink = ink[max(0, y - pad) : y + h + pad, max(0, x - pad) : x + w + pad]
        holes = []
        edge = shape.reshape(-1, 2).astype(float)
        for cx, cy, r in _circles(crop_ink, line):
            gx, gy = cx + max(0, x - pad), cy + max(0, y - pad)
            lx, ly = int(gx - x + 1), int(gy - y + 1)
            if not (0 <= ly < filled.shape[0] and 0 <= lx < filled.shape[1]):
                continue
            if not filled[ly, lx] or r < 2 * line:
                continue
            gap = float(np.min(np.hypot(edge[:, 0] - (gx - x + 1), edge[:, 1] - (gy - y + 1))))
            if gap < r + 0.5 * line:
                continue
            holes.append((float(gx), float(gy), float(r)))
        found.append(
            Outline(
                points=points,
                box=centre_box,
                holes=holes,
                line=line,
                ink=crop_ink,
                ink_origin=(max(0, x - pad), max(0, y - pad)),
                filled=filled,
                filled_origin=(x - 1, y - 1),
            )
        )
    found.sort(key=lambda o: -o.box[2] * o.box[3])
    return found


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / float(aw * ah + bw * bh - inter or 1)


def _match(value: float, labels: list[float], share: float = 0.02) -> float | None:
    best = min(labels, key=lambda v: abs(v - value), default=None)
    if best is not None and abs(best - value) <= share * best:
        return best
    return None


def find_labelled_holes(
    outline: Outline, diameters: list[float], scale: float
) -> list[tuple[float, float, float]]:
    """Второй, прицельный проход: окружности радиуса надписи Ø.

    Отверстие, которое пересекают выноска и стрелка (планка: Ø10 у
    скругления R10), общий поиск пропускает; инженер знает из надписи, что
    такое отверстие есть, и ищет окружность именно этого размера (порог
    мягче, кольцо чернил проверяется так же).
    """
    from app.ai.cad_views.view_features import _circles

    if outline.ink is None or not diameters:
        return []
    added: list[tuple[float, float, float]] = []
    ix, iy = outline.ink_origin
    fx, fy = outline.filled_origin
    for diameter in sorted(set(diameters)):
        radius = diameter / 2.0 / scale
        if radius < 2 * outline.line:
            continue
        for cx, cy, r in _circles(
            outline.ink, outline.line, threshold=10, radius=(0.85 * radius, 1.15 * radius)
        ):
            gx, gy = cx + ix, cy + iy
            lx, ly = int(gx - fx), int(gy - fy)
            if not (0 <= ly < outline.filled.shape[0] and 0 <= lx < outline.filled.shape[1]):
                continue
            if not outline.filled[ly, lx] or abs(2 * r * scale - diameter) > 0.06 * diameter:
                continue
            if any(
                math.hypot(gx - hx, gy - hy) < max(r, hr) for hx, hy, hr in outline.holes + added
            ):
                continue
            added.append((float(gx), float(gy), float(r)))
    outline.holes.extend(added)
    return added


def fit_plan_scale(
    outline: Outline, linear: list[float], diameters: list[float]
) -> tuple[float, float, int, list[str]] | None:
    """(мм/px по x, по y, число объяснённых надписей, какие) — лучшая пара."""
    x, y, w, h = outline.box
    labels = sorted({v for v in linear if v > 0})
    best: tuple[int, float, float, float, list[str]] | None = None
    for a in labels:
        for b in labels:
            sx, sy = a / w, b / h
            if abs(sx / sy - 1.0) > 0.04:
                continue
            scale = (sx + sy) / 2.0
            explained = {f"ширина {a:g}", f"высота {b:g}"}
            residual = 0.0
            for cx, cy, r in outline.holes:
                hit = _match(2 * r * scale, diameters, 0.03)
                if hit is not None:
                    explained.add(f"Ø{hit:g}")
                for distance in (
                    (cx - x) * sx,
                    (x + w - cx) * sx,
                    (y + h - cy) * sy,
                    (cy - y) * sy,
                ):
                    hit = _match(distance, labels)
                    if hit is not None and hit not in (a, b):
                        explained.add(f"положение {hit:g}")
            for (cx, _cy, _r), (dx, _dy, _q) in (
                (p, q) for i, p in enumerate(outline.holes) for q in outline.holes[i + 1 :]
            ):
                hit = _match(abs(cx - dx) * sx, labels)
                if hit is not None:
                    explained.add(f"между отверстиями {hit:g}")
            residual += abs(sx - sy)
            key = (len(explained), -residual)
            if best is None or key > (best[0], -best[1]):
                best = (len(explained), residual, sx, sy, sorted(explained))
    if best is None:
        return None
    return best[2], best[3], best[0], best[4]


def plan_sketch(
    outline: Outline, sx: float, sy: float
) -> tuple[list[dict[str, Any]], tuple[float, float]]:
    """Эскиз ядра: отрезки от первой вершины (0, 0), y вверх; начало в px."""
    points = outline.points
    x, y, w, h = outline.box
    # Первая вершина — ближайшая к левому нижнему углу габарита.
    start = min(
        range(len(points)), key=lambda i: math.hypot(points[i][0] - x, points[i][1] - (y + h))
    )
    ordered = points[start:] + points[:start]
    ox, oy = ordered[0]
    mm = [((px - ox) * sx, (oy - py) * sy) for px, py in ordered]
    # Почти горизонтальные и вертикальные стороны — точно по осям.
    snapped = [mm[0]]
    for px, py in mm[1:]:
        lx, ly = snapped[-1]
        if abs(py - ly) <= 0.03 * max(abs(px - lx), 1e-6):
            py = ly
        elif abs(px - lx) <= 0.03 * max(abs(py - ly), 1e-6):
            px = lx
        if math.hypot(px - lx, py - ly) > 0.05:
            snapped.append((px, py))
    segments = [{"kind": "line", "to": [round(px, 4), round(py, 4)]} for px, py in snapped[1:]]
    segments.append({"kind": "line", "to": [0.0, 0.0]})
    return segments, (ox, oy)


def build_extrude(
    gray: Any,
    reading: Any,
    label_texts: list[str],
    *,
    region_labels: dict[int, list[str]] | None = None,
    part: str | None = None,
) -> Any:
    """Выдавливание по контуру вида в плане; ``ViewsResult`` как у тела вращения."""
    from app.ai.cad_views.labels import parse_label
    from app.ai.cad_views.pipeline import ViewsResult

    texts = list(label_texts)
    for extra in (region_labels or {}).values():
        texts.extend(extra)
    parsed = [parse_label(t) for t in texts]
    linear = [lab.value for lab in parsed if lab.kind == "linear" and lab.value]
    diameters = [lab.value for lab in parsed if lab.kind in ("diameter", "thread") and lab.value]
    thickness_labels = [lab.value for lab in parsed if lab.kind == "thickness" and lab.value]
    boxes = [tuple(r.box) for r in reading.regions if r.role in ("view", "section")]
    if not boxes:
        return ViewsResult(False, "на листе не найдено изображения детали")
    found = outlines(gray, boxes)
    if not found:
        return ViewsResult(False, "замкнутого контура основной линии не найдено")
    tried: list[str] = []
    best = None
    for outline in found[:4]:
        fit = fit_plan_scale(outline, linear, diameters)
        if fit is None:
            tried.append(f"контур {outline.box[2]}×{outline.box[3]} px: пары надписей нет")
            continue
        sx, sy, hits, what = fit
        tried.append(f"контур {outline.box[2]}×{outline.box[3]} px: {', '.join(what)}")
        if hits < 3:
            continue
        if best is None or hits > best[3]:
            best = (outline, sx, sy, hits, what)
    if best is None:
        return ViewsResult(
            False,
            "контур детали не объяснён надписями (нужны ширина, высота и ещё одна): "
            + "; ".join(tried),
        )
    outline, sx, sy, hits, what = best
    notes = ["контур: " + "; ".join(tried)]
    added = find_labelled_holes(outline, diameters, (sx + sy) / 2.0)
    if added:
        notes.append(f"отверстий найдено по надписям Ø: {len(added)}")
    # Толщина: надпись «sN», иначе второй вид в проекционной связи.
    thickness = thickness_labels[0] if thickness_labels else None
    source = "надпись s"
    if thickness is None:
        x, y, w, h = outline.box
        for other in found:
            if other is outline:
                continue
            ox, oy, ow, oh = other.box
            same_columns = min(x + w, ox + ow) - max(x, ox) >= 0.8 * w
            same_rows = min(y + h, oy + oh) - max(y, oy) >= 0.8 * h
            measured = oh * sy if same_columns else ow * sx if same_rows else None
            if measured is None or measured >= 0.8 * min(w * sx, h * sy):
                continue
            thickness = _match(measured, linear, 0.05) or round(measured, 2)
            source = "второй вид"
            break
    if thickness is None:
        return ViewsResult(
            False, "толщина не найдена: нет надписи «sN» и второго вида", notes=notes
        )
    segments, (ox, oy) = plan_sketch(outline, sx, sy)
    features: list[dict[str, Any]] = [
        {
            "kind": "extrude",
            "params": {"sketch_profile": segments, "depth_mm": thickness},
            "confidence": 0.6,
        }
    ]
    scale = (sx + sy) / 2.0
    for cx, cy, r in outline.holes:
        diameter = _match(2 * r * scale, diameters, 0.06) or round(2 * r * scale, 2)
        features.append(
            {
                "kind": "hole",
                "params": {
                    "diameter_mm": diameter,
                    "center_x_mm": round((cx - ox) * sx, 4),
                    "center_y_mm": round((oy - cy) * sy, 4),
                    "through": True,
                },
                "confidence": 0.6,
            }
        )
    candidate = {
        "candidate": {
            "features": features,
            "score": 0.6,
            "label": part or "деталь",
        },
        "confirm_assumptions": True,
        "metadata": {"source": "cad_views", "method": "extrude"},
    }
    notes.append(f"толщина {thickness:g} мм ({source})")
    return ViewsResult(
        True,
        candidate=candidate,
        profile={"kind": "extrude", "sketch": segments, "thickness_mm": thickness},
        features=[f for f in features if f["kind"] == "hole"],
        scales={"x_mm_per_px": sx, "y_mm_per_px": sy, "labels_explained": hits},
        notes=notes,
    )


__all__ = ["Outline", "build_extrude", "fit_plan_scale", "main_line_mask", "outlines"]
