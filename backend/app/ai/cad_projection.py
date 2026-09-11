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


def _arc_image_angles(
    center: Any,
    first: Any,
    last: Any,
    mid: Any = None,
) -> tuple[float, float]:
    """Углы дуги в кадре IR (y вниз), от начала к концу по возрастанию угла.

    Ядро отдаёт дугу центром, радиусом и двумя концами — направления в этом
    нет. Прежний перевод приводил оба угла к 0…360° и СОРТИРОВАЛ их, отбрасывая
    направление: четверть окружности от 0° до 270° (то есть до −90°) выходила
    дугой на 270°. Так рисовался правый конец каждой прорези и каждый
    скруглённый угол пластины — в PNG, SVG и DXF.

    Точка на середине дуги снимает неоднозначность полностью. Без неё берётся
    меньшая дуга (≤ 180°): так нарисована любая прорезь и скругление, а дуги
    больше полуокружности ядро теперь отдаёт вместе с серединой. Конец может
    быть больше 360°: переход через 0° честно остаётся переходом.
    """
    import math

    cu, cv = (float(value) for value in center)

    def angle(point: Any) -> float:
        # Кадр IR — y вниз, поэтому v отражается ДО atan2, а не после.
        return math.degrees(math.atan2(-(float(point[1]) - cv), float(point[0]) - cu)) % 360.0

    a_first, a_last = angle(first), angle(last)
    forward = (a_last - a_first) % 360.0
    if mid is not None:
        through_mid = (angle(mid) - a_first) % 360.0 <= forward
    else:
        through_mid = forward <= 180.0
    if through_mid:
        return a_first, a_first + forward
    return a_last, a_last + (360.0 - forward)


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
            start, end = _arc_image_angles(
                item["center"],
                item["points"][0],
                item["points"][-1],
                item.get("mid"),
            )
            cu, cv = item["center"]
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
            "offset_v": placements["front"]["offset_v"] + front_height + VIEW_GAP_MM + tb["v_max"],
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


def place_sheet_views(
    views: list[dict[str, Any]],
    *,
    px_per_mm: float,
    origin_u_mm: float = 0.0,
    origin_v_mm: float = 0.0,
    gap_mm: float = VIEW_GAP_MM,
    skip: set[int] | None = None,
) -> tuple[list[Any], list[dict[str, float] | None]]:
    """Lay out the views ``/drawing`` returned, in the order it returned them.

    ``place_views`` takes a dict keyed by direction, which cannot express what a
    real sheet needs: a SECTION is not a fourth direction, it is what stands in
    the front view's place on a hollow part, and a sheet may carry two of them.
    Placements come back as a LIST parallel to the input, because that is how
    dimensions address their view (``view_index``).

    The anchor view is the first non-section view, or the first view when every
    one of them is a section. A section replaces the anchor position when it
    cuts along the same direction; further views go right, then below, in ГОСТ
    2.305 first-angle alignment.
    """
    placements: list[dict[str, float] | None] = [None] * len(views)
    if not views:
        return [], placements
    # ``skip`` is for a view the KERNEL needed but the sheet must not carry: a
    # section requires a base view to cut, and on a hollow part that base view
    # would otherwise appear beside its own section — the same part drawn twice.
    # It keeps its index so dimensions still address their view correctly.
    skipped = skip or set()

    def bounds(view: dict[str, Any]) -> dict[str, float] | None:
        value = view.get("bounds_mm")
        return value if isinstance(value, dict) and value else None

    anchor_index = next(
        (
            index
            for index, view in enumerate(views)
            if index not in skipped and view.get("kind") != "section" and bounds(view)
        ),
        next(
            (index for index, view in enumerate(views) if index not in skipped and bounds(view)),
            None,
        ),
    )
    if anchor_index is None:
        return [], placements

    anchor = bounds(views[anchor_index])
    assert anchor is not None
    anchor_width = anchor["u_max"] - anchor["u_min"]
    anchor_height = anchor["v_max"] - anchor["v_min"]
    anchor_axis_v = (anchor["v_max"] + anchor["v_min"]) / 2.0
    placements[anchor_index] = {
        "offset_u": origin_u_mm - anchor["u_min"],
        "offset_v": origin_v_mm + anchor["v_max"],
    }

    right_edge_mm = origin_u_mm + anchor_width
    bottom_edge_mm = origin_v_mm + anchor_height
    for index, view in enumerate(views):
        if index == anchor_index or index in skipped:
            continue
        box = bounds(view)
        if box is None:
            continue
        kind = view.get("kind")
        if kind == "top":
            # Directly below the anchor, sharing its u — first-angle.
            placements[index] = {
                "offset_u": origin_u_mm - box["u_min"],
                "offset_v": bottom_edge_mm + gap_mm + box["v_max"],
            }
            bottom_edge_mm += gap_mm + (box["v_max"] - box["v_min"])
            continue
        # A side view or a section goes to the right, on the anchor's axis —
        # the identity that makes this a projection rather than a second
        # drawing of the same part.
        axis_v = (box["v_max"] + box["v_min"]) / 2.0
        placements[index] = {
            "offset_u": right_edge_mm + gap_mm - box["u_min"],
            "offset_v": placements[anchor_index]["offset_v"] - anchor_axis_v + axis_v,
        }
        right_edge_mm += gap_mm + (box["u_max"] - box["u_min"])

    entities: list[Any] = []
    for index, view in enumerate(views):
        placement = placements[index]
        if placement is None:
            continue
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


def sheet_extent_mm(
    views: list[dict[str, Any]], placements: list[dict[str, float] | None]
) -> tuple[float, float]:
    """How much paper the placed views occupy, in view millimetres."""
    us: list[float] = []
    vs: list[float] = []
    for view, placement in zip(views, placements, strict=False):
        box = view.get("bounds_mm")
        if not placement or not isinstance(box, dict) or not box:
            continue
        us += [placement["offset_u"] + box["u_min"], placement["offset_u"] + box["u_max"]]
        vs += [placement["offset_v"] - box["v_max"], placement["offset_v"] - box["v_min"]]
    if not us or not vs:
        return 0.0, 0.0
    return max(us) - min(us), max(vs) - min(vs)


# TechDraw's dimension types, mapped onto what the IR calls them. A diameter on
# a longitudinal view is measured as a DistanceY between the two generatrices,
# so the caller states the intent and it must survive into the IR.
_DIMENSION_KINDS = {
    "Diameter": "diameter",
    "Radius": "radial",
    "Distance": "linear",
    "DistanceX": "linear",
    "DistanceY": "linear",
}


# ГОСТ 2.307 dimension appearance, in millimetres of paper.
DIM_OFFSET_MM = 8.0  # dimension line stands off the measured feature
DIM_EXTENSION_MM = 2.0  # extension line runs past the dimension line
DIM_ARROW_MM = 3.5
DIM_ARROW_GAP_MM = 1.0  # bare line wanted between two inside arrowheads
DIM_TEXT_MM = 3.5


# Шаг между рядами размеров, мм листа (ГОСТ 2.307: не менее 7 мм между
# параллельными размерными линиями).
DIM_TIER_STEP_MM = 7.0


def _projected_dimension_points(
    kind: str,
    first: tuple[float, float],
    second: tuple[float, float],
    *,
    top: float | None = None,
    tier: int = 0,
    place_u: float | None = None,
) -> tuple[tuple[tuple[float, float], tuple[float, float]], ...]:
    """Where the dimension line is measured from, per the kind TechDraw was asked.

    Returns ``((anchor, base), (anchor, base))``: the witness line runs from the
    anchor, the dimension line between the bases.

    **DistanceX** (a length along the axis). Anchors of a DistanceX between the
    end faces of a stepped shaft sit at different heights, and drawing straight
    between them put the overall length on a DIAGONAL across the part (live:
    «103» from (1012,803) to (1823,850) on a synthetic shaft). Levelling it at
    the higher anchor was not enough either: TechDraw puts anchors somewhere on
    the end faces, not at the top of the outline, so the line still ran INSIDE
    the part, through the keyway. A length is placed outside the view — above
    its top (``top``) — and ``tier`` lifts it into its own row when it would
    overlap another length (the overall length over the chain).

    **DistanceY** (a diameter across a longitudinal view). Drawn across the
    part inside its own step, per ГОСТ 2.307: halfway between the anchors.
    Pushing it left of the leftmost anchor — my first attempt — moved Ø25 onto
    the Ø28 step and stacked the two labels on top of each other.

    Anything else keeps the old behaviour: point to point.
    """
    (u1, v1), (u2, v2) = first, second
    if kind == "DistanceX":
        if u1 > u2:
            (u1, v1), (u2, v2) = (u2, v2), (u1, v1)
        level = (top if top is not None else max(v1, v2)) + tier * DIM_TIER_STEP_MM
        return ((u1, v1), (u1, level)), ((u2, v2), (u2, level))
    if kind == "DistanceY":
        if v1 > v2:
            (u1, v1), (u2, v2) = (u2, v2), (u1, v1)
        # Лучше всего — место, которое знает запрос: середина участка, где есть
        # обе образующие. Иначе — середина между привязками.
        level = place_u if place_u is not None else (u1 + u2) / 2.0
        return ((u1, v1), (level, v1)), ((u2, v2), (level, v2))
    return ((u1, v1), (u1, v1)), ((u2, v2), (u2, v2))


def _length_tiers(dimensions: list[dict[str, Any]]) -> dict[int, int]:
    """Row for each DistanceX on its view: overlapping lengths never share a row.

    Greedy by span, shortest first, so the chain sits nearest the part and the
    overall length — which overlaps every link of the chain — goes outside.
    Links that merely TOUCH at a shoulder share a row: that is what a chain is.
    """
    by_view: dict[Any, list[tuple[float, float, int]]] = {}
    for position, item in enumerate(dimensions):
        anchors = item.get("anchors_mm") or []
        if str(item.get("kind") or "") != "DistanceX" or len(anchors) < 2:
            continue
        lo, hi = sorted((float(anchors[0][0]), float(anchors[1][0])))
        by_view.setdefault(item.get("view_index"), []).append((lo, hi, position))
    tiers: dict[int, int] = {}
    for spans in by_view.values():
        rows: list[list[tuple[float, float]]] = []
        for lo, hi, position in sorted(spans, key=lambda item: item[1] - item[0]):
            for row_index, row in enumerate(rows):
                if all(hi <= a + 1e-6 or lo >= b - 1e-6 for a, b in row):
                    row.append((lo, hi))
                    tiers[position] = row_index
                    break
            else:
                rows.append([(lo, hi)])
                tiers[position] = len(rows) - 1
    return tiers


def dimensions_from_kernel(
    dimensions: list[dict[str, Any]],
    placements: dict[str, dict[str, float]],
    view_order: list[str],
    *,
    px_per_mm: float,
) -> list[Any]:
    """Kernel-measured dimensions → a drawn ГОСТ 2.307 dimension on the sheet.

    The VALUE and the anchor points come from TechDraw measuring the solid, so
    a dimension cannot disagree with the geometry it labels — which is the
    point of taking them from the model instead of restating the spec. What
    TechDraw will not give headless is the APPEARANCE: getArrowPositions
    returns zeros because arrows are placed by its GUI renderer. So the
    witness lines, the dimension line, the arrowheads and the text are drawn
    here, and before this they were a single bare line between two points with
    no arrows and no value on it.
    """
    import math

    from app.ai.cad_ir.schema import DimensionEntity, TextEntity
    from app.ai.cad_semantics import parse_dimension

    entities: list[Any] = []
    tiers = _length_tiers(dimensions)
    for position, item in enumerate(dimensions):
        anchors = item.get("anchors_mm") or []
        if len(anchors) < 2:
            continue
        index = int(item.get("view_index") or 0)
        if index >= len(view_order):
            continue
        placement = placements.get(view_order[index])
        if not placement:
            continue

        def to_point(u: float, v: float):
            return Point(
                x=(placement["offset_u"] + u) * px_per_mm,
                y=(placement["offset_v"] - v) * px_per_mm,
            )

        bounds = placement.get("bounds_mm") or {}
        anchor_1, anchor_2 = _projected_dimension_points(
            str(item.get("kind") or ""),
            (float(anchors[0][0]), float(anchors[0][1])),
            (float(anchors[1][0]), float(anchors[1][1])),
            top=float(bounds["v_max"]) if "v_max" in bounds else None,
            tier=tiers.get(position, 0),
            place_u=float(item["place_u"])
            if isinstance(item.get("place_u"), (int, float))
            else None,
        )
        (a1u, a1v), (u1, v1) = anchor_1
        (a2u, a2v), (u2, v2) = anchor_2
        du, dv = u2 - u1, v2 - v1
        span = math.hypot(du, dv)
        if span <= 1e-6:
            continue
        # Offset the dimension line perpendicular to what is being measured,
        # away from the part: a dimension drawn ON the contour is unreadable.
        nu, nv = -dv / span, du / span
        # Диаметр окружности идёт ЧЕРЕЗ центр, по самой окружности — без
        # отступа и без выносных, как по ГОСТ 2.307. Сдвинутый в сторону, он
        # мерил бы хорду, а не диаметр.
        through_centre = str(item.get("kind") or "") == "Diameter"
        offset = 0.0 if through_centre else DIM_OFFSET_MM
        extension = 0.0 if through_centre else DIM_EXTENSION_MM
        ou, ov = nu * offset, nv * offset
        eu, ev = nu * (offset + extension), nv * (offset + extension)

        style = {"line_class": "dim", "width_class": "thin", **_ORIGIN}
        tu, tv = du / span, dv / span
        # Two arrowheads that do not fit between the witness lines go OUTSIDE
        # them, and the dimension line is carried past (ГОСТ 2.307). Drawn
        # inside, a 6 mm dimension was two arrows fused into a bow tie with no
        # line left at all — unreadable to a person and unmeasurable to the
        # dimension-line verifier (40 % of plate and flange lengths).
        outside = span < 2.0 * DIM_ARROW_MM + DIM_ARROW_GAP_MM
        carry = DIM_ARROW_MM + DIM_EXTENSION_MM if outside else 0.0
        # Witness lines from the feature out past the dimension line. They
        # start at the ORIGINAL anchor: for a DistanceX between end faces at
        # different heights the witness lines differ in length, and that is
        # exactly what keeps the dimension line itself horizontal.
        if not through_centre:
            entities.append(Segment(p1=to_point(a1u, a1v), p2=to_point(u1 + eu, v1 + ev), **style))
            entities.append(Segment(p1=to_point(a2u, a2v), p2=to_point(u2 + eu, v2 + ev), **style))
        # The dimension line itself.
        entities.append(
            Segment(
                p1=to_point(u1 + ou - tu * carry, v1 + ov - tv * carry),
                p2=to_point(u2 + ou + tu * carry, v2 + ov + tv * carry),
                **style,
            )
        )
        # Arrowheads: a closed sliver at each end, pointing outward — or, when
        # they do not fit, standing outside and pointing in.
        inward = -1.0 if outside else 1.0
        for sign, (bu, bv) in ((1.0, (u1 + ou, v1 + ov)), (-1.0, (u2 + ou, v2 + ov))):
            tip_u, tip_v = bu, bv
            back_u = bu + inward * sign * tu * DIM_ARROW_MM
            back_v = bv + inward * sign * tv * DIM_ARROW_MM
            wing = DIM_ARROW_MM * 0.28
            entities.append(
                Polyline(
                    points=[
                        to_point(tip_u, tip_v),
                        to_point(back_u + nu * wing, back_v + nv * wing),
                        to_point(back_u - nu * wing, back_v - nv * wing),
                    ],
                    closed=True,
                    **style,
                )
            )

        value = item.get("value_mm")
        label = str(item.get("label") or "")
        # TechDraw may return either a prefix ("Ø") or the complete formatted
        # callout ("Ø56.55", "M75x1,5").  Appending the measured value to the
        # latter produced Ø56.5556.55, 1717 and M75x1,575 on real sheets.
        text = (
            (label if any(character.isdigit() for character in label) else f"{label}{value:g}")
            if isinstance(value, (int, float))
            else label
        )
        semantic = parse_dimension(text) or {}
        if text:
            # Подпись диаметра окружности — не в центре, а ближе к концу линии:
            # в центре сходятся все концентрические размеры.
            share = 0.75 if through_centre else 0.5
            mid_u = u1 + (u2 - u1) * share + ou + nu * 1.5
            mid_v = v1 + (v2 - v1) * share + ov + nv * 1.5
            entities.append(
                TextEntity(
                    position=to_point(mid_u, mid_v),
                    text=text,
                    height=DIM_TEXT_MM * px_per_mm,
                    rotation=0.0,
                    **style,
                )
            )
        # The semantic entity stays: the DXF export and the reviewers read it,
        # and it is what makes this a dimension rather than four strokes.
        entities.append(
            DimensionEntity(
                p1=to_point(u1, v1),
                p2=to_point(u2, v2),
                text=text,
                # What KIND of size this is, carried through from the kernel. It
                # was dropped, so every dimension reached the IR as "linear" —
                # a Ø102 measured between two generatrices exported to DXF as a
                # plain distance, and downstream anything reading the IR saw a
                # part with no diameters at all.
                kind=str(
                    item.get("ir_kind")
                    or _DIMENSION_KINDS.get(str(item.get("kind") or ""), "linear")
                ),
                value_mm=float(value) if isinstance(value, (int, float)) else None,
                fit=semantic.get("fit"),
                deviation=semantic.get("deviation"),
                thread=semantic.get("thread"),
                tolerance=semantic.get("fit") or semantic.get("deviation"),
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
    views: dict[str, dict[str, Any]],
    report: dict[str, Any],
    *,
    part_class: str = "rotation",
    scale: float = 1.0,
) -> dict[str, Any]:
    """Do the derived views measure the same part the kernel built?

    A projection cannot drift on its own, but a wrong view FRAME (a swapped
    axis, a stale mapping) silently produces a plausible drawing of the wrong
    thing — which is exactly the failure this project keeps paying for. So the
    view extents are checked against the solid's bounding box.

    What "front" should measure depends on the part. On a shaft it is length by
    diameter; on a flange, seen down its own axis, the front view is diameter by
    diameter and the old rule called every correct flange sheet wrong. When the
    class is unknown the check reports the numbers and claims nothing.
    """
    bounds = report.get("bounds_mm") or {}
    # ``/drawing`` returns each view already multiplied by the sheet scale,
    # while the solid is measured in real millimetres. Comparing the two
    # directly failed every correctly drawn sheet that was not 1:1 — a 470 mm
    # shaft at 1:2.5 reads 188 mm on paper, and it should.
    ratio = float(scale) if scale and scale > 0 else 1.0
    length = float(bounds.get("z") or 0.0) * ratio
    diameter = max(float(bounds.get("x") or 0.0), float(bounds.get("y") or 0.0)) * ratio
    checks: dict[str, Any] = {"ok": True, "part_class": part_class, "scale": ratio}

    if part_class in ("flange", "plate"):
        # Seen along the axis: both extents are the outline, and the thickness
        # is what a side view or a section shows instead.
        front = (views.get("front") or {}).get("bounds_mm")
        if front:
            front_u = front["u_max"] - front["u_min"]
            front_v = front["v_max"] - front["v_min"]
            checks["front_width_mm"] = round(front_u, 3)
            checks["front_height_mm"] = round(front_v, 3)
            checks["front_matches_solid"] = abs(front_u - diameter) <= max(
                0.05, diameter * 0.005
            ) and abs(front_v - diameter) <= max(0.05, diameter * 0.005)
            checks["ok"] = checks["ok"] and checks["front_matches_solid"]
        checks["expected_thickness_mm"] = round(length, 3)
        checks["expected_diameter_mm"] = round(diameter, 3)
        return checks

    front = (views.get("front") or {}).get("bounds_mm")
    if front:
        front_u = front["u_max"] - front["u_min"]
        front_v = front["v_max"] - front["v_min"]
        checks["front_length_mm"] = round(front_u, 3)
        checks["front_height_mm"] = round(front_v, 3)
        checks["front_matches_solid"] = abs(front_u - length) <= max(0.05, length * 0.005) and abs(
            front_v - diameter
        ) <= max(0.05, diameter * 0.005)
        checks["ok"] = checks["ok"] and checks["front_matches_solid"]

    side = (views.get("side") or {}).get("bounds_mm")
    if side:
        side_u = side["u_max"] - side["u_min"]
        side_v = side["v_max"] - side["v_min"]
        checks["side_width_mm"] = round(side_u, 3)
        checks["side_matches_solid"] = abs(side_u - diameter) <= max(
            0.05, diameter * 0.005
        ) and abs(side_v - diameter) <= max(0.05, diameter * 0.005)
        checks["ok"] = checks["ok"] and checks["side_matches_solid"]

    # Both in paper millimetres, i.e. already scaled — the same units the
    # view extents above are reported in.
    checks["expected_length_mm"] = round(length, 3)
    checks["expected_diameter_mm"] = round(diameter, 3)
    return checks
