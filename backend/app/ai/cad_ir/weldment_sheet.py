"""Лист сварного узла (X3): размеры пластин, положения рёбер, швы, позиции.

Виды заданы в `sheet_from_solid.plan_views`, их оси подобраны пробой ядра:
вид спереди — u = −x, v = z; план (наблюдатель на +Z) — u = −x, v = y; вид
слева — u = y, v = z. Числа кладутся на вид от его рамки: у рамки вида и у
габарита узла общие края, и отсчёт от них не зависит от того, где ядро
поставило начало координат вида.

Швы — по ГОСТ 2.312: полка-выноска от валика с обозначением стандарта, типа
шва и катета («ГОСТ 5264-80-Т1-△4»). Позиции — номера у тел со ссылкой на
перечень под видами (ГОСТ 2.109: сборочный чертёж узла).
"""

from __future__ import annotations

from typing import Any

# Отступ перечня и полок швов от видов, мм листа.
_BOM_GAP_MM = 12.0
_BOM_ROW_MM = 6.0
_SHELF_MM = 34.0
_LEADER_MM = 10.0


def _boxes(spec: dict) -> list[tuple[list[float], list[float]]] | None:
    from app.ai.cad_solid import _body_box

    boxes = []
    for body in spec.get("parts") or []:
        box = _body_box(body) if isinstance(body, dict) else None
        if box is None:
            return None
        boxes.append(box)
    return boxes or None


def _frames(spec: dict, plan: Any, views: list[dict]) -> dict[str, Any] | None:
    """Перевод точки узла (x, y, z) в координаты каждого из трёх видов."""
    boxes = _boxes(spec)
    if boxes is None:
        return None
    low = [min(box[0][axis] for box in boxes) for axis in range(3)]
    ratio = plan.ratio or 1.0
    index = {view["kind"]: i for i, view in enumerate(plan.views)}
    if not all(kind in index for kind in ("front", "plan", "top")):
        return None
    bounds = {
        kind: (views[index[kind]] or {}).get("bounds_mm") for kind in ("front", "plan", "top")
    }
    if not all(isinstance(value, dict) and value for value in bounds.values()):
        return None
    front, above, left = bounds["front"], bounds["plan"], bounds["top"]
    return {
        "boxes": boxes,
        "index": index,
        "front": lambda x, z: (
            front["u_max"] - (x - low[0]) * ratio,
            front["v_min"] + (z - low[2]) * ratio,
        ),
        "plan": lambda x, y: (
            above["u_max"] - (x - low[0]) * ratio,
            above["v_min"] + (y - low[1]) * ratio,
        ),
        "left": lambda y, z: (
            left["u_min"] + (y - low[1]) * ratio,
            left["v_min"] + (z - low[2]) * ratio,
        ),
    }


def _dimension(view_index: int, kind: str, first, second, value: float, by: str, **extra) -> dict:
    return {
        "view_index": view_index,
        "kind": kind,
        "label": f"{value:g}",
        "anchors_mm": [list(first), list(second)],
        "value_mm": round(value, 3),
        "measured_by": by,
        "ir_kind": "linear",
        **extra,
    }


def weldment_dimensions(drawing: dict, spec: dict, plan: Any) -> None:
    """Размеры узла: основание (ширина, глубина, толщина), рёбра (высота,
    толщина, положение от кромки основания)."""
    if plan.part_class != "weldment":
        return
    frames = _frames(spec, plan, drawing.get("views") or [])
    if frames is None:
        return
    dimensions = drawing.setdefault("dimensions", [])
    boxes = frames["boxes"]
    (blo, bhi) = boxes[0]
    left, front = frames["left"], frames["front"]
    left_index, front_index = frames["index"]["top"], frames["index"]["front"]
    width = bhi[0] - blo[0]
    depth = bhi[1] - blo[1]
    thickness = bhi[2] - blo[2]
    dimensions.append(
        _dimension(
            front_index,
            "DistanceX",
            front(bhi[0], bhi[2]),
            front(blo[0], bhi[2]),
            width,
            "weldment_width",
        )
    )
    dimensions.append(
        _dimension(
            left_index,
            "DistanceX",
            left(blo[1], blo[2]),
            left(bhi[1], blo[2]),
            depth,
            "weldment_depth",
            below=True,
        )
    )
    dimensions.append(
        _dimension(
            left_index,
            "DistanceY",
            left(blo[1], blo[2]),
            left(blo[1], bhi[2]),
            thickness,
            "weldment_thickness",
        )
    )
    for lo, hi in boxes[1:]:
        dimensions.append(
            _dimension(
                left_index,
                "DistanceY",
                left(hi[1], lo[2]),
                left(hi[1], hi[2]),
                hi[2] - lo[2],
                "weldment_rib_height",
                below=True,
            )
        )
        dimensions.append(
            _dimension(
                left_index,
                "DistanceX",
                left(lo[1], hi[2]),
                left(hi[1], hi[2]),
                hi[1] - lo[1],
                "weldment_rib_thickness",
            )
        )
        if lo[1] - blo[1] > 1e-6:
            dimensions.append(
                _dimension(
                    left_index,
                    "DistanceX",
                    left(blo[1], blo[2]),
                    left(lo[1], lo[2]),
                    lo[1] - blo[1],
                    "weldment_rib_position",
                    below=True,
                )
            )


def weld_designation(weld: dict) -> str:
    """Обозначение шва на полке выноски (ГОСТ 2.312): стандарт-тип-△катет."""
    parts = [str(weld.get("standard") or "ГОСТ 5264-80"), str(weld.get("designation") or "")]
    leg = weld.get("leg_mm")
    if isinstance(leg, (int, float)):
        parts.append(f"△{leg:g}")
    return "-".join(part for part in parts if part)


def weldment_entities(
    spec: dict, plan: Any, views: list[dict], placements: list[dict | None]
) -> list[Any]:
    """Полки швов и номера позиций на виде слева, перечень под видами."""
    from app.ai.cad_ir.schema import Point, Segment, TextEntity
    from app.ai.cad_ir.sheet_from_solid import PAPER_PX_PER_MM
    from app.ai.cad_projection import _ORIGIN, DIM_TEXT_MM

    if plan.part_class != "weldment":
        return []
    frames = _frames(spec, plan, views)
    if frames is None:
        return []
    left_index = frames["index"]["top"]
    placement = placements[left_index] if left_index < len(placements) else None
    if not placement:
        return []
    left = frames["left"]
    boxes = frames["boxes"]

    def point(u: float, v: float) -> Point:
        # Координаты вида → лист (y листа растёт вниз).
        return Point(
            x=(placement["offset_u"] + u) * PAPER_PX_PER_MM,
            y=(placement["offset_v"] - v) * PAPER_PX_PER_MM,
        )

    def text(u: float, v: float, value: str, *, anchor: str = "start") -> TextEntity:
        return TextEntity(
            position=point(u, v),
            text=value,
            height=DIM_TEXT_MM * PAPER_PX_PER_MM,
            rotation=0.0,
            anchor=anchor,
            line_class="dim",
            width_class="thin",
            **_ORIGIN,
        )

    def thin(first: tuple[float, float], second: tuple[float, float]) -> Segment:
        return Segment(
            p1=point(*first), p2=point(*second), line_class="dim", width_class="thin", **_ORIGIN
        )

    entities: list[Any] = []
    bounds = views[left_index].get("bounds_mm") or {}
    top = float(bounds.get("v_max") or 0.0)
    for number, weld in enumerate(spec.get("welds") or []):
        pair = weld.get("bodies") or []
        if len(pair) != 2 or not all(0 <= int(i) < len(boxes) for i in pair):
            continue
        (lo, hi) = boxes[int(pair[1])]
        base_top = boxes[int(pair[0])][1][2]
        # Валик — у ближней к началу стенки ребра (сторона −y), на основании.
        foot = left(lo[1], base_top)
        # Над размером толщины ребра (он стоит на 8 мм над видом).
        shelf_v = top + 2.0 * _LEADER_MM + number * _BOM_ROW_MM
        elbow = (foot[0] - _LEADER_MM, shelf_v)
        entities.append(thin(foot, elbow))
        entities.append(thin(elbow, (elbow[0] - _SHELF_MM, shelf_v)))
        entities.append(text(elbow[0] - _SHELF_MM, shelf_v + 1.0, weld_designation(weld)))
    # Номера позиций — на телах, перечень — под видами.
    for position, (lo, hi) in enumerate(boxes, start=1):
        # У правого края тела над его верхом внутри: у основания середину
        # занимает размер высоты ребра.
        mark = left(lo[1] + 0.85 * (hi[1] - lo[1]), lo[2] + 0.5 * (hi[2] - lo[2]))
        entities.append(text(mark[0] + 1.5, mark[1] - 1.5, str(position)))
    # Перечень — от нижнего края самого нижнего вида.
    bottom = max(
        placement_item["offset_v"] - float((view.get("bounds_mm") or {}).get("v_min") or 0.0)
        for view, placement_item in zip(views, placements, strict=False)
        if placement_item
    )
    x0 = min(
        placement_item["offset_u"] + float((view.get("bounds_mm") or {}).get("u_min") or 0.0)
        for view, placement_item in zip(views, placements, strict=False)
        if placement_item
    )
    for row, body in enumerate(spec.get("parts") or [], start=1):
        profile = body.get("profile") or {}
        size = "×".join(
            f"{float(profile.get(key) or 0):g}" for key in ("width_mm", "height_mm", "thickness_mm")
        )
        line = f"Поз. {row} — {body.get('name') or 'Пластина'} {size}"
        entities.append(
            TextEntity(
                position=Point(
                    x=x0 * PAPER_PX_PER_MM,
                    y=(bottom + _BOM_GAP_MM + row * _BOM_ROW_MM) * PAPER_PX_PER_MM,
                ),
                text=line,
                height=DIM_TEXT_MM * PAPER_PX_PER_MM,
                rotation=0.0,
                anchor="start",
                line_class="dim",
                width_class="thin",
                **_ORIGIN,
            )
        )
    return entities


def weldment_extra_height_mm(spec: dict, plan: Any) -> float:
    """Место под перечнем позиций."""
    if plan.part_class != "weldment":
        return 0.0
    return _BOM_GAP_MM + (len(spec.get("parts") or []) + 1) * _BOM_ROW_MM
