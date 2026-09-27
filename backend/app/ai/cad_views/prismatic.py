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


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def fit_edges_scale(
    outlines_: list[Any], linear: list[float], diameters: list[float]
) -> tuple[float, list[str]] | None:
    """мм/px по расстояниям между кромками контуров видов и Ø отверстий.

    Габарит контура плана включает приливы («80» надписан по телу, а контур
    шире на прилив), поэтому пара «ширина/высота контура» подбиралась
    случайная. Кандидаты — надпись / расстояние между вертикальными или
    горизонтальными кромками любого вида; побеждает масштаб, при котором
    больше разных надписей совпадает, при равенстве — более крупные."""
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
    best: tuple[int, float, float, list[str]] | None = None
    for label in labels[:25]:
        for distance in distances:
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
            key = (len(hits), weight)
            if best is None or key > (best[0], best[1]):
                best = (len(hits), weight, scale, sorted(hits))
    if best is None:
        return None
    return best[2], best[3]


def arrange_views(outlines: list[Any], main: Any) -> dict[str, Any]:
    """Виды в проекционной связи с главным: {"below", "right", "above", "left"}."""
    mx, my, mw, mh = main.box
    placed: dict[str, Any] = {}
    for outline in outlines:
        if outline is main:
            continue
        x, y, w, h = outline.box
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
        if side not in placed or gap < placed[side][0]:
            placed[side] = (gap, outline)
    return {side: item[1] for side, item in placed.items()}


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
    boxes = [tuple(r.box) for r in reading.regions if r.role in ("view", "section")]
    if not boxes:
        return ViewsResult(False, "на листе не найдено изображения детали")
    # Стрелки размеров и цифры у кромок — толстые пятна: размыкание шире.
    found = outlines(gray, boxes, opening_lines=5.0)
    if len(found) < 2:
        return ViewsResult(False, "для призматической детали нужно не меньше двух видов")
    main_box = next((r.box for r in reading.regions if r.n == reading.main), None)

    def overlaps_main(outline: Any) -> float:
        if main_box is None:
            return 0.0
        x, y, w, h = outline.box
        bx0, by0, bx1, by1 = main_box
        return _overlap(x, x + w, bx0, bx1) * _overlap(y, y + h, by0, by1)

    candidates = sorted(found[:6], key=lambda o: (-overlaps_main(o), -o.box[2] * o.box[3]))
    tried: list[str] = []
    best = None
    for main in candidates[:3]:
        views = arrange_views(found, main)
        if not views:
            tried.append(f"контур {main.box[2]}×{main.box[3]} px: нет видов в проекционной связи")
            continue
        fit = fit_edges_scale([main, *views.values()], linear, diameters)
        if fit is None:
            tried.append(f"контур {main.box[2]}×{main.box[3]} px: надписи не объясняют кромок")
            continue
        scale, what = fit
        # Размер вида вглубь — ещё одна надпись в пользу масштаба.
        for side, outline in views.items():
            extent = (outline.box[3] if side in ("below", "above") else outline.box[2]) * scale
            hit = _match(extent, linear, 0.03)
            if hit is not None:
                what = [*what, f"глубина {hit:g}"]
        tried.append(f"контур {main.box[2]}×{main.box[3]} px: {', '.join(what)}")
        key = (len(views), len(what))
        if len(what) >= 3 and (best is None or key > best[0]):
            best = (key, main, views, scale, what)
    if best is None:
        return ViewsResult(
            False,
            "призматическая деталь не объяснена видами и надписями: " + "; ".join(tried),
        )
    _key, main, views, scale, what = best
    mx, my, mw, mh = main.box
    # Габарит по оси — наименьший из видов, которые его показывают: контур
    # раздувают стрелки и цифры у кромок, но никогда не уменьшают (корпус:
    # главный вид 99,8 при боковом 90).
    widths = [mw] + [views[k].box[2] for k in ("below", "above") if k in views]
    heights = [mh] + [views[k].box[3] for k in ("right", "left") if k in views]
    depths = [views[k].box[3] for k in ("below", "above") if k in views] + [
        views[k].box[2] for k in ("right", "left") if k in views
    ]
    width, height, depth = (
        min(widths) * scale,
        min(heights) * scale,
        min(depths) * scale,
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


__all__ = ["arrange_views", "build_prismatic", "view_polygon"]
