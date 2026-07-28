"""Deterministic dimension-constraint graph for CAD reader output.

The graph does not infer geometry.  It turns already-read values into named
nodes and checks only arithmetic/topological facts that must hold before a
feature tree is compiled.
"""

from __future__ import annotations

import re
from typing import Any


_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def build_dimension_graph(spec: dict) -> dict[str, Any]:
    """Return nodes, constraints and blocking contradictions."""
    body = spec.get("main_view") or {}
    outer = body.get("outer") or []
    bore = body.get("bore") or []
    nodes: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    errors: list[str] = []

    def sections(group: str, items: list[dict]) -> float:
        total = 0.0
        for index, item in enumerate(items):
            diameter = _number(item.get("diameter_mm", item.get("d")))
            length = _number(item.get("length_mm", item.get("l")))
            for field, value in (("diameter_mm", diameter), ("length_mm", length)):
                nodes.append({
                    "id": f"main_view.{group}.{index}.{field}",
                    "value_mm": value,
                    "evidence": item.get("evidence") or [],
                    "status": "read" if value is not None else "missing",
                })
            if length is not None:
                total += length
        return total

    outer_total = sections("outer", outer)
    bore_total = sections("bore", bore)
    constraints.append({
        "kind": "sum",
        "target": "outer_total_length_mm",
        "value_mm": outer_total,
        "members": [f"main_view.outer.{i}.length_mm" for i in range(len(outer))],
        "ok": bool(outer) and outer_total > 0,
    })

    stated_overall = None
    internal_callouts: list[tuple[int, float, str]] = []
    for index, dimension in enumerate(spec.get("dimensions") or []):
        applies_to = str(dimension.get("applies_to") or "").lower()
        value = _number(dimension.get("value"))
        nodes.append({
            "id": f"dimensions.{index}",
            "value_mm": value,
            "raw": dimension.get("value"),
            "applies_to": dimension.get("applies_to"),
            "evidence": dimension.get("evidence") or [],
            "status": "read" if value is not None else "unparsed",
        })
        if value is not None and any(word in applies_to for word in ("габарит", "overall", "общая длина")):
            stated_overall = value
        if value is not None and any(
            word in applies_to for word in ("расточ", "внутрен", "конус")
        ):
            internal_callouts.append((index, value, applies_to))

    if stated_overall is not None and outer_total > 0:
        ok = abs(stated_overall - outer_total) <= max(0.05, stated_overall * 0.005)
        constraints.append({
            "kind": "equal",
            "left": "outer_total_length_mm",
            "right": "stated_overall_length_mm",
            "left_value_mm": outer_total,
            "right_value_mm": stated_overall,
            "ok": ok,
        })
        if not ok:
            errors.append(
                f"габаритная длина {stated_overall:g} мм не равна сумме наружных ступеней {outer_total:g} мм"
            )

    if bore and outer_total > 0:
        ok = bore_total <= outer_total + 0.05
        constraints.append({
            "kind": "less_or_equal",
            "left": "bore_total_length_mm",
            "right": "outer_total_length_mm",
            "left_value_mm": bore_total,
            "right_value_mm": outer_total,
            "ok": ok,
        })
        if not ok:
            errors.append(
                f"длина внутреннего профиля {bore_total:g} мм превышает длину детали {outer_total:g} мм"
            )
    elif internal_callouts:
        errors.append(
            "на листе прочитаны размеры внутренней геометрии, но внутренний профиль bore[] отсутствует"
        )

    for group in ("cross_holes", "keyways", "grooves"):
        for index, feature in enumerate(body.get(group) or []):
            position = _number(
                feature.get("axial_position_mm", feature.get("axial_start_mm"))
            )
            if position is None:
                continue
            length = _number(feature.get("length_mm")) or 0.0
            ok = 0 <= position and position + length <= outer_total + 0.05
            constraints.append({
                "kind": "inside",
                "feature": f"main_view.{group}.{index}",
                "start_mm": position,
                "end_mm": position + length,
                "body_length_mm": outer_total,
                "ok": ok,
            })
            if not ok:
                errors.append(
                    f"{group}[{index}] расположен вне длины детали: {position:g}..{position + length:g} мм"
                )

    return {
        "status": "ok" if not errors else "conflict",
        "nodes": nodes,
        "constraints": constraints,
        "errors": errors,
    }
