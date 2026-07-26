"""Stage 2 of the 3D-first redraw: views DERIVED from the solid.

Once the model is right, ГОСТ 2.305 projection alignment stops being something
a drafter has to maintain and becomes arithmetic: every view is the same solid
seen from a different direction, so two views cannot disagree about a diameter.
The hand-built left view of the 2D drafter was correct only for stepped shafts;
this path is correct for whatever the kernel can build.

Line types follow ГОСТ 2.303 straight from the projector's own classification:
visible silhouette is a contour line, occluded geometry is a dashed thin one.
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_ir.schema import Arc, Circle, Point, Polyline, Segment

# Gap between neighbouring views, in millimetres of the part.
VIEW_GAP_MM = 20.0

_ORIGIN = {"origin": "spec", "assurance": "constraint_validated"}


class ProjectionMismatch(RuntimeError):
    """A derived view does not measure what the solid measures."""


def _entities_from_items(
    items: list[dict[str, Any]],
    *,
    hidden: bool,
    px_per_mm: float,
    offset_u: float,
    offset_v: float,
) -> list[Any]:
    """Projected primitives → CAD IR entities in sheet pixels.

    ``v`` is negated: the projector works in a maths frame (y up) while the IR
    canvas is y-down, and a silently flipped view would be a mirrored part.
    """
    line_class = "hidden" if hidden else "contour"
    width_class = "thin" if hidden else "main"
    style = {"line_class": line_class, "width_class": width_class, **_ORIGIN}

    def to_px(u: float, v: float) -> Point:
        return Point(x=(offset_u + u) * px_per_mm, y=(offset_v - v) * px_per_mm)

    entities: list[Any] = []
    for item in items:
        kind = item.get("type")
        if kind == "line":
            (u1, v1), (u2, v2) = item["points"]
            entities.append(Segment(p1=to_px(u1, v1), p2=to_px(u2, v2), **style))
        elif kind == "circle":
            cu, cv = item["center"]
            entities.append(
                Circle(
                    center=to_px(cu, cv),
                    radius=float(item["radius"]) * px_per_mm,
                    **style,
                )
            )
        elif kind == "arc":
            cu, cv = item["center"]
            (u1, v1), (u2, v2) = item["points"][0], item["points"][-1]
            import math

            # Angles are measured in the flipped (y-down) frame the IR uses, so
            # they are computed AFTER the flip rather than converted afterwards.
            start = math.degrees(math.atan2(-(v1 - cv), u1 - cu)) % 360.0
            end = math.degrees(math.atan2(-(v2 - cv), u2 - cu)) % 360.0
            if end < start:
                start, end = end, start
            entities.append(
                Arc(
                    center=to_px(cu, cv),
                    radius=float(item["radius"]) * px_per_mm,
                    start_angle=start,
                    end_angle=end,
                    **style,
                )
            )
        elif kind == "polyline":
            points = [to_px(u, v) for u, v in item["points"]]
            if len(points) >= 2:
                entities.append(Polyline(points=points, closed=False, **style))
    return entities


def place_views(
    views: dict[str, dict[str, Any]],
    *,
    px_per_mm: float,
    origin_u_mm: float = 0.0,
    origin_v_mm: float = 0.0,
) -> tuple[list[Any], dict[str, dict[str, float]]]:
    """Lay derived views out in ГОСТ 2.305 first-angle alignment.

    The front view anchors the sheet; the left view goes to its RIGHT sharing
    the same horizontal axis, the top view BELOW it sharing the same vertical
    axis. Alignment is computed from the projector's own bounds, so the views
    stay in projection no matter what the part is.
    """
    front = views.get("front")
    if not front or not front.get("bounds_mm"):
        return [], {}
    fb = front["bounds_mm"]
    front_width = fb["u_max"] - fb["u_min"]
    front_height = fb["v_max"] - fb["v_min"]
    front_axis_v = (fb["v_max"] + fb["v_min"]) / 2.0

    # An offset maps view coordinates onto the sheet: sheet_u = offset_u + u,
    # sheet_v = offset_v - v (the canvas is y-down).
    placements: dict[str, dict[str, float]] = {
        "front": {
            "offset_u": origin_u_mm - fb["u_min"],
            "offset_v": origin_v_mm + fb["v_max"],
        }
    }

    side = views.get("side")
    if side and side.get("bounds_mm"):
        sb = side["bounds_mm"]
        side_axis_v = (sb["v_max"] + sb["v_min"]) / 2.0
        placements["side"] = {
            "offset_u": origin_u_mm + front_width + VIEW_GAP_MM - sb["u_min"],
            # Same axis line as the front view — the identity that makes this a
            # projection rather than a second drawing of the same part.
            "offset_v": placements["front"]["offset_v"] - front_axis_v + side_axis_v,
        }

    top = views.get("top")
    if top and top.get("bounds_mm"):
        tb = top["bounds_mm"]
        placements["top"] = {
            # Directly below the front view and sharing its u — first-angle.
            "offset_u": origin_u_mm - tb["u_min"],
            "offset_v": placements["front"]["offset_v"] + front_height + VIEW_GAP_MM
            + tb["v_max"],
        }

    entities: list[Any] = []
    for name, placement in placements.items():
        view = views[name]
        for hidden in (False, True):
            items = view.get("hidden" if hidden else "visible") or []
            entities.extend(
                _entities_from_items(
                    items,
                    hidden=hidden,
                    px_per_mm=px_per_mm,
                    offset_u=placement["offset_u"],
                    offset_v=placement["offset_v"],
                )
            )
        entities.extend(
            _hatch_from_outlines(
                view.get("hatch") or [],
                px_per_mm=px_per_mm,
                offset_u=placement["offset_u"],
                offset_v=placement["offset_v"],
            )
        )
    return entities, placements


def dimensions_from_kernel(
    dimensions: list[dict[str, Any]],
    placements: dict[str, dict[str, float]],
    view_order: list[str],
    *,
    px_per_mm: float,
) -> list[Any]:
    """Kernel-measured dimensions → IR dimension entities on the sheet.

    The VALUE and the anchor points come from TechDraw measuring the solid, so
    a dimension cannot disagree with the geometry it labels — which is the
    whole point of taking them from the model instead of restating the spec.
    """
    from app.ai.cad_ir.schema import DimensionEntity

    entities: list[Any] = []
    for item in dimensions:
        anchors = item.get("anchors_mm") or []
        if len(anchors) < 2:
            continue
        index = int(item.get("view_index") or 0)
        if index >= len(view_order):
            continue
        placement = placements.get(view_order[index])
        if not placement:
            continue
        value = item.get("value_mm")
        label = str(item.get("label") or "")
        text = f"{label}{value:g}" if isinstance(value, (int, float)) else label
        (u1, v1), (u2, v2) = anchors[0], anchors[1]
        entities.append(
            DimensionEntity(
                p1=Point(
                    x=(placement["offset_u"] + float(u1)) * px_per_mm,
                    y=(placement["offset_v"] - float(v1)) * px_per_mm,
                ),
                p2=Point(
                    x=(placement["offset_u"] + float(u2)) * px_per_mm,
                    y=(placement["offset_v"] - float(v2)) * px_per_mm,
                ),
                text=text,
                value_mm=float(value) if isinstance(value, (int, float)) else None,
                **_ORIGIN,
            )
        )
    return entities


def _hatch_from_outlines(
    outlines: list[list[list[float]]],
    *,
    px_per_mm: float,
    offset_u: float,
    offset_v: float,
) -> list[Any]:
    """Cut-material outlines → ГОСТ 2.306 hatch regions, in sheet pixels.

    The kernel returns the material the section plane passes through, holes
    already excluded, so each outline is filled as it stands. ``v`` is negated
    for the same reason the edges are: the projector works y-up and the IR
    canvas is y-down.
    """
    from app.ai.cad_ir.schema import HatchRegion

    regions: list[Any] = []
    for outline in outlines:
        points = [
            Point(x=(offset_u + float(u)) * px_per_mm, y=(offset_v - float(v)) * px_per_mm)
            for u, v in outline
        ]
        if len(points) >= 3:
            regions.append(HatchRegion(boundary=points, pattern="ansi31", **_ORIGIN))
    return regions


def verify_views_against_solid(
    views: dict[str, dict[str, Any]], report: dict[str, Any]
) -> dict[str, Any]:
    """Do the derived views measure the same part the kernel built?

    A projection cannot drift on its own, but a wrong view FRAME (a swapped
    axis, a stale mapping) silently produces a plausible drawing of the wrong
    thing — which is exactly the failure this project keeps paying for. So the
    view extents are checked against the solid's bounding box.
    """
    bounds = report.get("bounds_mm") or {}
    length = float(bounds.get("z") or 0.0)
    diameter = max(float(bounds.get("x") or 0.0), float(bounds.get("y") or 0.0))
    checks: dict[str, Any] = {"ok": True}

    front = (views.get("front") or {}).get("bounds_mm")
    if front:
        front_u = front["u_max"] - front["u_min"]
        front_v = front["v_max"] - front["v_min"]
        checks["front_length_mm"] = round(front_u, 3)
        checks["front_height_mm"] = round(front_v, 3)
        checks["front_matches_solid"] = (
            abs(front_u - length) <= max(0.05, length * 0.005)
            and abs(front_v - diameter) <= max(0.05, diameter * 0.005)
        )
        checks["ok"] = checks["ok"] and checks["front_matches_solid"]

    side = (views.get("side") or {}).get("bounds_mm")
    if side:
        side_u = side["u_max"] - side["u_min"]
        side_v = side["v_max"] - side["v_min"]
        checks["side_width_mm"] = round(side_u, 3)
        checks["side_matches_solid"] = (
            abs(side_u - diameter) <= max(0.05, diameter * 0.005)
            and abs(side_v - diameter) <= max(0.05, diameter * 0.005)
        )
        checks["ok"] = checks["ok"] and checks["side_matches_solid"]

    checks["solid_length_mm"] = round(length, 3)
    checks["solid_diameter_mm"] = round(diameter, 3)
    return checks
