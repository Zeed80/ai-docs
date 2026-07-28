"""Deterministic dimension-constraint graph for CAD reader output.

The graph does not infer geometry.  It turns already-read values into named
nodes and checks only arithmetic/topological facts that must hold before a
feature tree is compiled.
"""

from __future__ import annotations

import re
from typing import Any


_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_SYMMETRIC_TOLERANCE = re.compile(r"±\s*(\d+(?:[.,]\d+)?)")


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


def _tolerance_interval(
    nominal_mm: float, tolerance: Any
) -> tuple[float | None, float | None, str]:
    """Resolve only tolerance forms whose numeric interval is unambiguous."""
    text = str(tolerance or "").strip()
    if not text:
        return None, None, "not_stated"
    symmetric = _SYMMETRIC_TOLERANCE.fullmatch(text)
    if symmetric:
        delta = float(symmetric.group(1).replace(",", "."))
        return delta, -delta, "stated"
    from app.ai.cad_machining import deviations_from_fit

    return deviations_from_fit(nominal_mm, text)


def _circle_fits_rounded_rectangle(
    x: float,
    y: float,
    feature_radius: float,
    width: float,
    height: float,
    corner_radius: float,
) -> bool:
    """Exact containment of a circular footprint in a centred rounded box."""
    half_width, half_height = width / 2.0, height / 2.0
    if feature_radius < 0 or corner_radius < 0:
        return False
    if abs(x) + feature_radius > half_width + 0.05:
        return False
    if abs(y) + feature_radius > half_height + 0.05:
        return False
    if corner_radius <= 0:
        return True
    arc_x, arc_y = half_width - corner_radius, half_height - corner_radius
    if abs(x) <= arc_x or abs(y) <= arc_y:
        return True
    distance = ((abs(x) - arc_x) ** 2 + (abs(y) - arc_y) ** 2) ** 0.5
    return distance + feature_radius <= corner_radius + 0.05


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
            tolerance = item.get("tolerance")
            if diameter is not None and tolerance:
                upper, lower, source = _tolerance_interval(diameter, tolerance)
                # k6/p7 and similar fits have a known IT grade width but need
                # a fundamental-deviation table before their interval can be
                # located around nominal. They are valid, explicitly partial
                # constraints — not contradictions and not invented limits.
                grade_only = source.startswith("grade_only_it")
                ok = grade_only or (
                    upper is not None and lower is not None and lower <= upper
                )
                constraints.append({
                    "kind": "tolerance_interval",
                    "target": f"main_view.{group}.{index}.diameter_mm",
                    "nominal_mm": diameter,
                    "tolerance": tolerance,
                    "upper_deviation_mm": upper,
                    "lower_deviation_mm": lower,
                    "source": source,
                    "interval_complete": not grade_only,
                    "ok": ok,
                })
                if ok and not grade_only:
                    nodes.extend([
                        {
                            "id": f"main_view.{group}.{index}.diameter_min_mm",
                            "value_mm": diameter + float(lower),
                            "status": "derived",
                        },
                        {
                            "id": f"main_view.{group}.{index}.diameter_max_mm",
                            "value_mm": diameter + float(upper),
                            "status": "derived",
                        },
                    ])
                elif not ok:
                    errors.append(
                        f"{group}[{index}] содержит неподдержанный или неполный допуск {tolerance!r}"
                    )
            if length is not None:
                start = total
                total += length
                nodes.extend([
                    {"id": f"main_view.{group}.{index}.z_start_mm", "value_mm": start, "status": "derived"},
                    {"id": f"main_view.{group}.{index}.z_end_mm", "value_mm": total, "status": "derived"},
                ])
                taper = item.get("taper") or {}
                if taper:
                    end_diameter = _number(taper.get("end_diameter_mm"))
                    ratio = str(taper.get("ratio") or "")
                    ratio_parts = [_number(value) for value in ratio.split(":", 1)]
                    if end_diameter is not None:
                        ok = end_diameter > 0
                        constraints.append({
                            "kind": "positive_taper_end",
                            "section": f"main_view.{group}.{index}",
                            "value_mm": end_diameter,
                            "ok": ok,
                        })
                        if not ok:
                            errors.append(f"{group}[{index}] задаёт неположительный конечный диаметр конуса")
                    elif len(ratio_parts) == 2 and None not in ratio_parts:
                        numerator, denominator = ratio_parts
                        ok = bool(numerator and denominator and numerator > 0 and denominator > 0)
                        constraints.append({
                            "kind": "valid_taper_ratio",
                            "section": f"main_view.{group}.{index}",
                            "ratio": ratio,
                            "ok": ok,
                        })
                        if not ok:
                            errors.append(f"{group}[{index}] содержит недопустимое обозначение конусности {ratio!r}")
        return total

    def outer_diameter_at(position: float) -> float | None:
        cursor = 0.0
        for item in outer:
            length = _number(item.get("length_mm", item.get("l")))
            diameter = _number(item.get("diameter_mm", item.get("d")))
            if length is None:
                continue
            if cursor - 0.05 <= position <= cursor + length + 0.05:
                return diameter
            cursor += length
        return None

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
        bore_start = _number(body.get("bore_start_mm")) or 0.0
        ok = bore_start + bore_total <= outer_total + 0.05
        constraints.append({
            "kind": "less_or_equal",
            "left": "bore_start_plus_length_mm",
            "right": "outer_total_length_mm",
            "left_value_mm": bore_start + bore_total,
            "right_value_mm": outer_total,
            "ok": ok,
        })
        if not ok:
            errors.append(
                f"расточка {bore_start:g}..{bore_start + bore_total:g} мм выходит за длину детали {outer_total:g} мм"
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
            if group == "cross_holes":
                feature_diameter = _number(feature.get("diameter_mm"))
                local_diameter = outer_diameter_at(position)
                fits = (
                    feature_diameter is not None
                    and local_diameter is not None
                    and feature_diameter <= local_diameter + 0.05
                )
                constraints.append({
                    "kind": "diameter_inside",
                    "feature": f"main_view.{group}.{index}",
                    "feature_diameter_mm": feature_diameter,
                    "local_body_diameter_mm": local_diameter,
                    "ok": fits,
                })
                if not fits:
                    errors.append(
                        f"cross_holes[{index}] Ø{feature_diameter or 0:g} не помещается в локальный диаметр Ø{local_diameter or 0:g}"
                    )

    # A shoulder fillet must fit both the radial step and the two adjacent
    # axial runs. This is known before OpenCascade and is therefore a graph
    # contradiction, not a kernel warning to discover after the build.
    boundaries: list[tuple[float, int]] = []
    cursor = 0.0
    for index, item in enumerate(outer[:-1]):
        cursor += _number(item.get("length_mm", item.get("l"))) or 0.0
        boundaries.append((cursor, index))
    for index, fillet in enumerate(body.get("fillets") or []):
        radius = _number(fillet.get("radius_mm"))
        if radius is None or fillet.get("location") != "shoulder":
            continue
        at_z = _number(fillet.get("at_z_mm"))
        at_diameter = _number(fillet.get("at_diameter_mm"))
        boundary_index = None
        if at_z is not None:
            boundary_index = next(
                (item_index for z, item_index in boundaries if abs(z - at_z) <= 0.05),
                None,
            )
        elif at_diameter is not None:
            boundary_index = next(
                (
                    item_index for _z, item_index in boundaries
                    if abs((_number(outer[item_index].get("diameter_mm", outer[item_index].get("d"))) or 0.0) - at_diameter) <= 0.05
                ),
                None,
            )
        if boundary_index is None:
            continue
        left, right = outer[boundary_index], outer[boundary_index + 1]
        radial_step = abs(
            (_number(left.get("diameter_mm", left.get("d"))) or 0.0)
            - (_number(right.get("diameter_mm", right.get("d"))) or 0.0)
        ) / 2.0
        max_radius = min(
            radial_step,
            _number(left.get("length_mm", left.get("l"))) or 0.0,
            _number(right.get("length_mm", right.get("l"))) or 0.0,
        )
        ok = max_radius > 0 and radius <= max_radius + 0.05
        constraints.append({
            "kind": "fillet_fits_shoulder",
            "feature": f"main_view.fillets.{index}",
            "radius_mm": radius,
            "max_radius_mm": max_radius,
            "shoulder_z_mm": boundaries[boundary_index][0],
            "ok": ok,
        })
        if not ok:
            errors.append(
                f"fillets[{index}] R{radius:g} не помещается на уступе: максимум R{max_radius:g}"
            )

    profile = body.get("profile") or {}
    shape = profile.get("shape")
    corner_radius = _number(profile.get("corner_radius_mm")) or 0.0
    if shape == "rectangle" and corner_radius:
        width = _number(profile.get("width_mm")) or 0.0
        height = _number(profile.get("height_mm")) or 0.0
        max_corner_radius = min(width, height) / 2.0
        radius_ok = max_corner_radius > 0 and corner_radius <= max_corner_radius + 0.05
        constraints.append({
            "kind": "corner_radius_fits_profile",
            "target": "main_view.profile.corner_radius_mm",
            "radius_mm": corner_radius,
            "max_radius_mm": max_corner_radius,
            "ok": radius_ok,
        })
        if not radius_ok:
            errors.append(
                f"profile.corner_radius_mm R{corner_radius:g} не помещается в "
                f"профиль {width:g}×{height:g} мм: максимум R{max_corner_radius:g}"
            )
    for group in ("holes", "slots"):
        for index, feature in enumerate(profile.get(group) or []):
            x = _number(feature.get("center_x_mm"))
            y = _number(feature.get("center_y_mm"))
            if x is None or y is None:
                continue
            radius = (
                _number(feature.get("diameter_mm"))
                or _number(feature.get("length_mm"))
                or _number(feature.get("width_mm"))
                or 0.0
            ) / 2.0
            if shape == "rectangle":
                width = _number(profile.get("width_mm")) or 0.0
                height = _number(profile.get("height_mm")) or 0.0
                fits = _circle_fits_rounded_rectangle(
                    x, y, radius, width, height, corner_radius
                )
            elif shape == "circle":
                profile_radius = (_number(profile.get("diameter_mm")) or 0.0) / 2.0
                fits = (x * x + y * y) ** 0.5 + radius <= profile_radius + 0.05
            else:
                continue
            constraints.append({
                "kind": "inside_profile",
                "feature": f"main_view.profile.{group}.{index}",
                "center_mm": [x, y],
                "radius_mm": radius,
                "ok": fits,
            })
            if not fits:
                errors.append(
                    f"profile.{group}[{index}] с центром ({x:g}, {y:g}) мм выходит за контур детали"
                )

    return {
        "status": "ok" if not errors else "conflict",
        "nodes": nodes,
        "constraints": constraints,
        "errors": errors,
    }
