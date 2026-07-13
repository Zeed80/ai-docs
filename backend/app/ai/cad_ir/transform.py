"""Generic geometric editor operations over CAD IR entities (Ф5.6-5.8):
move/copy/mirror work structurally on whichever point-bearing fields an
entity has (p1/p2/center/points/boundary/position), so one implementation
covers Segment/Arc/Circle/Polyline/Text/Dimension/Hatch without a per-type
dispatch table. Fillet/chamfer are classic two-segment corner operations.

All of these return NEW entity objects (or tuples of them) — callers are
responsible for splicing them into ``ir.entities`` and re-validating; no
function here mutates its input or knows about the wider IR.
"""

from __future__ import annotations

import math

from app.ai.cad_ir.schema import Arc, Circle, Entity, Point, Segment, new_entity_id


def _translate_point(p: Point, dx: float, dy: float) -> Point:
    return Point(x=p.x + dx, y=p.y + dy)


def _mirror_point(p: Point, p1: Point, p2: Point) -> Point:
    """Reflect ``p`` across the infinite line through p1-p2."""
    dx, dy = p2.x - p1.x, p2.y - p1.y
    norm2 = dx * dx + dy * dy
    if norm2 < 1e-9:
        return Point(x=p.x, y=p.y)
    vx, vy = p.x - p1.x, p.y - p1.y
    t = (vx * dx + vy * dy) / norm2
    proj_x, proj_y = p1.x + t * dx, p1.y + t * dy
    return Point(x=2 * proj_x - p.x, y=2 * proj_y - p.y)


def _map_points(entity: Entity, fn) -> Entity:
    out = entity.model_copy(deep=True)
    for attr in ("p1", "p2", "center", "position"):
        val = getattr(out, attr, None)
        if val is not None:
            setattr(out, attr, fn(val))
    if getattr(out, "points", None):
        out.points = [fn(p) for p in out.points]
    if getattr(out, "boundary", None):
        out.boundary = [fn(p) for p in out.boundary]
    return out


def translate_entity(entity: Entity, dx: float, dy: float) -> Entity:
    out = _map_points(entity, lambda p: _translate_point(p, dx, dy))
    out.origin = "human"
    return out


def mirror_entity(entity: Entity, p1: Point, p2: Point) -> Entity:
    out = _map_points(entity, lambda p: _mirror_point(p, p1, p2))
    if isinstance(out, Arc):
        # Reflection reverses sweep direction: a point at angle theta (from
        # the now-mirrored center) maps to angle (2*phi - theta), where phi
        # is the mirror line's direction angle.
        phi_deg = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
        new_start = (2 * phi_deg - entity.end_angle) % 360
        new_end = (2 * phi_deg - entity.start_angle) % 360
        out.start_angle = new_start
        out.end_angle = new_end
    out.origin = "human"
    return out


def duplicate_entity(entity: Entity, dx: float = 0.0, dy: float = 0.0) -> Entity:
    """Copy with a fresh id (translated by dx/dy, which may be zero for an
    exact stack-on-top duplicate the user then drags)."""
    out = translate_entity(entity, dx, dy)
    out.id = new_entity_id()
    return out


class FilletChamferError(ValueError):
    """Segments aren't a valid corner (collinear, too far apart, or
    coincident) — surfaced to the caller as a typed 400, not a silent no-op
    or a wrong geometric guess."""


def _unit(dx: float, dy: float) -> tuple[float, float]:
    n = math.hypot(dx, dy)
    if n < 1e-9:
        raise FilletChamferError("degenerate segment (zero length)")
    return dx / n, dy / n


_CORNER_GAP_TOLERANCE_RATIO = 0.15  # corner endpoints may be up to 15% of the shorter segment's length apart


def _shared_and_far_endpoints(
    seg1: Segment, seg2: Segment
) -> tuple[Point, Point, Point]:
    """Which endpoint pair is the shared corner (closest pair), and the two
    "far" endpoints defining each segment's outward direction from it."""
    candidates = [
        (seg1.p1, seg1.p2, seg2.p1, seg2.p2),
        (seg1.p1, seg1.p2, seg2.p2, seg2.p1),
        (seg1.p2, seg1.p1, seg2.p1, seg2.p2),
        (seg1.p2, seg1.p1, seg2.p2, seg2.p1),
    ]
    best = min(candidates, key=lambda c: math.hypot(c[0].x - c[2].x, c[0].y - c[2].y))
    c1, f1, c2, f2 = best
    gap = math.hypot(c1.x - c2.x, c1.y - c2.y)
    len1 = math.hypot(f1.x - c1.x, f1.y - c1.y)
    len2 = math.hypot(f2.x - c2.x, f2.y - c2.y)
    if gap > _CORNER_GAP_TOLERANCE_RATIO * min(len1, len2):
        raise FilletChamferError("segments don't share a corner (endpoints too far apart)")
    corner = Point(x=(c1.x + c2.x) / 2, y=(c1.y + c2.y) / 2)
    return corner, f1, f2


def _replace_endpoint(seg: Segment, old: Point, new: Point) -> Segment:
    out = seg.model_copy(deep=True)
    d_p1 = math.hypot(out.p1.x - old.x, out.p1.y - old.y)
    d_p2 = math.hypot(out.p2.x - old.x, out.p2.y - old.y)
    if d_p1 <= d_p2:
        out.p1 = new
    else:
        out.p2 = new
    out.origin = "human"
    return out


def chamfer(seg1: Segment, seg2: Segment, distance: float) -> tuple[Segment, Segment, Segment]:
    if distance <= 0:
        raise FilletChamferError("chamfer distance must be positive")
    corner, far1, far2 = _shared_and_far_endpoints(seg1, seg2)
    u1x, u1y = _unit(far1.x - corner.x, far1.y - corner.y)
    u2x, u2y = _unit(far2.x - corner.x, far2.y - corner.y)
    a = Point(x=corner.x + distance * u1x, y=corner.y + distance * u1y)
    b = Point(x=corner.x + distance * u2x, y=corner.y + distance * u2y)
    new_seg1 = _replace_endpoint(seg1, corner, a)
    new_seg2 = _replace_endpoint(seg2, corner, b)
    bevel = Segment(
        p1=a, p2=b, line_class=seg1.line_class, width_class=seg1.width_class,
        origin="human", assurance="human_approved",
    )
    return new_seg1, new_seg2, bevel


def fillet(seg1: Segment, seg2: Segment, radius: float) -> tuple[Segment, Segment, Arc]:
    if radius <= 0:
        raise FilletChamferError("fillet radius must be positive")
    corner, far1, far2 = _shared_and_far_endpoints(seg1, seg2)
    u1x, u1y = _unit(far1.x - corner.x, far1.y - corner.y)
    u2x, u2y = _unit(far2.x - corner.x, far2.y - corner.y)
    cos_theta = max(-0.999999, min(0.999999, u1x * u2x + u1y * u2y))
    theta = math.acos(cos_theta)
    if theta < 1e-3 or theta > math.pi - 1e-3:
        raise FilletChamferError("segments are (nearly) collinear — cannot fillet")
    tan_dist = radius / math.tan(theta / 2)
    seg_len1 = math.hypot(far1.x - corner.x, far1.y - corner.y)
    seg_len2 = math.hypot(far2.x - corner.x, far2.y - corner.y)
    if tan_dist >= seg_len1 or tan_dist >= seg_len2:
        raise FilletChamferError("fillet radius too large for these segment lengths")
    t1 = Point(x=corner.x + tan_dist * u1x, y=corner.y + tan_dist * u1y)
    t2 = Point(x=corner.x + tan_dist * u2x, y=corner.y + tan_dist * u2y)
    bx, by = _unit(u1x + u2x, u1y + u2y)
    center_dist = radius / math.sin(theta / 2)
    center = Point(x=corner.x + center_dist * bx, y=corner.y + center_dist * by)
    start_angle = math.degrees(math.atan2(t1.y - center.y, t1.x - center.x)) % 360
    end_angle = math.degrees(math.atan2(t2.y - center.y, t2.x - center.x)) % 360
    # Always store the minor-arc ordering (the fillet is convex by
    # construction, so its true sweep is always < 180°).
    if (end_angle - start_angle) % 360 > 180:
        start_angle, end_angle = end_angle, start_angle
    new_seg1 = _replace_endpoint(seg1, corner, t1)
    new_seg2 = _replace_endpoint(seg2, corner, t2)
    arc = Arc(
        center=center, radius=radius, start_angle=start_angle, end_angle=end_angle,
        line_class=seg1.line_class, width_class=seg1.width_class,
        origin="human", assurance="human_approved",
    )
    return new_seg1, new_seg2, arc


# ── A2: sketch editing operations ────────────────────────────────────────────


class SketchOpError(ValueError):
    """A2 sketch operation can't be applied to this geometry — surfaced to the
    caller as a typed 422, never a silent no-op or a wrong geometric guess."""


def _line_intersection(a1: Point, a2: Point, b1: Point, b2: Point) -> Point | None:
    """Intersection of the two INFINITE lines through a1-a2 and b1-b2 (None if
    parallel)."""
    x1, y1, x2, y2 = a1.x, a1.y, a2.x, a2.y
    x3, y3, x4, y4 = b1.x, b1.y, b2.x, b2.y
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-9:
        return None
    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / den
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / den
    return Point(x=px, y=py)


def _param_on(seg: Segment, p: Point) -> float:
    """Projection parameter of ``p`` onto the segment's line (0 at p1, 1 at p2)."""
    dx, dy = seg.p2.x - seg.p1.x, seg.p2.y - seg.p1.y
    n2 = dx * dx + dy * dy
    if n2 < 1e-9:
        return 0.0
    return ((p.x - seg.p1.x) * dx + (p.y - seg.p1.y) * dy) / n2


def trim_segment(target: Segment, cutter: Segment, remove_point: Point) -> Segment:
    """Trim ``target`` back to where ``cutter`` crosses it, discarding the side
    that contains ``remove_point``. The cut must land within the target's own
    span — you can't trim to an edge that doesn't cross it."""
    ix = _line_intersection(target.p1, target.p2, cutter.p1, cutter.p2)
    if ix is None:
        raise SketchOpError("режущий отрезок параллелен — резать не к чему")
    t = _param_on(target, ix)
    if not (0.001 <= t <= 0.999):
        raise SketchOpError("отрезки не пересекаются — точки реза нет на этом отрезке")
    out = target.model_copy(deep=True)
    if _param_on(target, remove_point) >= t:
        out.p2 = ix  # remove_point sits past the cut toward p2 → keep p1..cut
    else:
        out.p1 = ix
    out.origin = "human"
    return out


def extend_segment(target: Segment, boundary: Segment, move_point: Point) -> Segment:
    """Extend ``target`` along its own line until it meets ``boundary``'s line;
    the endpoint nearest ``move_point`` is the one that moves. Must lengthen —
    if the intersection would shorten the segment, that's a trim, not an
    extend, and is refused."""
    ix = _line_intersection(target.p1, target.p2, boundary.p1, boundary.p2)
    if ix is None:
        raise SketchOpError("граница параллельна — продлевать некуда")
    out = target.model_copy(deep=True)
    d1 = math.hypot(move_point.x - target.p1.x, move_point.y - target.p1.y)
    d2 = math.hypot(move_point.x - target.p2.x, move_point.y - target.p2.y)
    old_len = math.hypot(target.p2.x - target.p1.x, target.p2.y - target.p1.y)
    if d1 <= d2:
        new_len = math.hypot(target.p2.x - ix.x, target.p2.y - ix.y)
        moved = out.model_copy(update={"p1": ix})
    else:
        new_len = math.hypot(ix.x - target.p1.x, ix.y - target.p1.y)
        moved = out.model_copy(update={"p2": ix})
    if new_len <= old_len + 1e-6:
        raise SketchOpError("пересечение укорачивает отрезок — это обрезка, не продление")
    moved.origin = "human"
    return moved


def offset_entity(entity: Entity, distance: float, side_point: Point) -> Entity:
    """Parallel copy of a segment/circle/arc at ``distance``, on the side of
    ``side_point``. A fresh id — the source stays."""
    if distance <= 0:
        raise SketchOpError("смещение должно быть положительным")
    if isinstance(entity, Segment):
        ux, uy = _unit(entity.p2.x - entity.p1.x, entity.p2.y - entity.p1.y)
        nx, ny = -uy, ux  # left normal
        mx, my = (entity.p1.x + entity.p2.x) / 2, (entity.p1.y + entity.p2.y) / 2
        sign = 1.0 if (side_point.x - mx) * nx + (side_point.y - my) * ny >= 0 else -1.0
        ox, oy = nx * distance * sign, ny * distance * sign
        out = entity.model_copy(
            update={
                "id": new_entity_id(),
                "p1": Point(x=entity.p1.x + ox, y=entity.p1.y + oy),
                "p2": Point(x=entity.p2.x + ox, y=entity.p2.y + oy),
                "origin": "human",
            }
        )
        return out
    if isinstance(entity, (Circle, Arc)):
        d = math.hypot(side_point.x - entity.center.x, side_point.y - entity.center.y)
        sign = 1.0 if d > entity.radius else -1.0
        new_r = entity.radius + sign * distance
        if new_r <= 1e-6:
            raise SketchOpError("смещение внутрь больше радиуса")
        return entity.model_copy(
            update={"id": new_entity_id(), "radius": new_r, "origin": "human"}
        )
    raise SketchOpError("смещение поддерживается для отрезка, окружности и дуги")


def _rotate_point(p: Point, center: Point, ang_rad: float) -> Point:
    dx, dy = p.x - center.x, p.y - center.y
    c, s = math.cos(ang_rad), math.sin(ang_rad)
    return Point(x=center.x + dx * c - dy * s, y=center.y + dx * s + dy * c)


def rotate_entity(entity: Entity, center: Point, ang_deg: float) -> Entity:
    """Copy of ``entity`` rotated by ``ang_deg`` about ``center`` (fresh id)."""
    a = math.radians(ang_deg)
    out = _map_points(entity, lambda p: _rotate_point(p, center, a))
    if isinstance(out, Arc):
        out.start_angle = (entity.start_angle + ang_deg) % 360
        out.end_angle = (entity.end_angle + ang_deg) % 360
    out.id = new_entity_id()
    out.origin = "human"
    return out


def pattern_linear(entity: Entity, count: int, dx: float, dy: float) -> list[Entity]:
    """``count`` total instances (including the original) spaced by (dx, dy);
    returns the NEW copies only (indices 1..count-1)."""
    if count < 2:
        raise SketchOpError("нужно минимум 2 экземпляра")
    return [duplicate_entity(entity, dx * i, dy * i) for i in range(1, count)]


def pattern_polar(
    entity: Entity, count: int, center: Point, total_angle_deg: float
) -> list[Entity]:
    """``count`` total instances around ``center`` spread over
    ``total_angle_deg`` (360 = full circle); returns the NEW copies only. Full
    turns divide by count so copies don't double up at 0°/360°; a partial fan
    divides by count-1 so the ends sit exactly on the span."""
    if count < 2:
        raise SketchOpError("нужно минимум 2 экземпляра")
    full = abs(abs(total_angle_deg) - 360.0) < 1e-6
    step = total_angle_deg / count if full else total_angle_deg / (count - 1)
    return [rotate_entity(entity, center, step * i) for i in range(1, count)]
