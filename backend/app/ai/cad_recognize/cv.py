"""Classical-CV recognizer: skeleton tracing + primitive fitting.

Thin adapter over ``drawing_vectorize.extract_primitives`` that converts its
``RawPrimitive`` structures into CAD IR entities. This backend is the
always-available fallback and the scaffolding the vertical was built on; the
neural seq2seq backend replaces it as the primary path without changing this
contract.
"""

from __future__ import annotations

import math
from typing import Any

import structlog

from app.ai.cad_ir.schema import Arc, Circle, Entity, HatchRegion, Point, Polyline, Segment
from app.ai.cad_recognize.base import RecognizeOutput
from app.ai.drawing_vectorize import RawPrimitive, extract_primitives

logger = structlog.get_logger()


def _to_entity(prim: RawPrimitive) -> Entity | None:
    # Line-class heuristic for revision 0: thick strokes are contours, thin
    # ones auxiliary. Axis/dim/hidden refinement is the VLM/review stage's job.
    common = {
        "line_class": "contour" if prim.is_thick else "thin",
        "width_class": "main" if prim.is_thick else "thin",
        "confidence": prim.confidence,
        "origin": "cv",
    }
    if prim.kind == "segment":
        return Segment(
            p1=Point(x=prim.p1[0], y=prim.p1[1]),
            p2=Point(x=prim.p2[0], y=prim.p2[1]),
            **common,
        )
    if prim.kind == "circle":
        return Circle(center=Point(x=prim.center[0], y=prim.center[1]), radius=prim.radius, **common)
    if prim.kind == "arc":
        return Arc(
            center=Point(x=prim.center[0], y=prim.center[1]),
            radius=prim.radius,
            start_angle=prim.start_angle,
            end_angle=prim.end_angle,
            **common,
        )
    if prim.kind == "polyline":
        return Polyline(
            points=[Point(x=x, y=y) for x, y in prim.points],
            closed=prim.closed,
            **common,
        )
    # "dot": single-pixel speck — noise at IR level, stays raster-only
    return None


_ARC_MERGE_CENTER_TOL_PX = 4.0
_ARC_MERGE_RADIUS_TOL = 0.05  # relative
_FULL_CIRCLE_MIN_SPAN_DEG = 330.0


def _merge_cocircular_arcs(entities: list[Entity]) -> list[Entity]:
    """Skeletonization often splits one drawn circle into several arcs (spur
    junctions break the loop). Arcs sharing a center/radius whose combined
    angular span is nearly full are one circle — merge them so the DXF gets a
    single CIRCLE entity instead of four arc fragments."""
    arcs = [e for e in entities if isinstance(e, Arc)]
    if len(arcs) < 2:
        return entities
    used: set[str] = set()
    merged: list[Entity] = []
    for i, a in enumerate(arcs):
        if a.id in used:
            continue
        group = [a]
        for b in arcs[i + 1:]:
            if b.id in used:
                continue
            dc = ((a.center.x - b.center.x) ** 2 + (a.center.y - b.center.y) ** 2) ** 0.5
            if dc <= _ARC_MERGE_CENTER_TOL_PX and abs(a.radius - b.radius) <= _ARC_MERGE_RADIUS_TOL * a.radius:
                group.append(b)
        span = sum(abs(g.end_angle - g.start_angle) for g in group)
        if len(group) >= 2 and span >= _FULL_CIRCLE_MIN_SPAN_DEG:
            for g in group:
                used.add(g.id)
            merged.append(
                Circle(
                    center=Point(
                        x=sum(g.center.x for g in group) / len(group),
                        y=sum(g.center.y for g in group) / len(group),
                    ),
                    radius=sum(g.radius for g in group) / len(group),
                    line_class=group[0].line_class,
                    width_class=group[0].width_class,
                    confidence=min(g.confidence for g in group),
                    origin="cv",
                )
            )
    if not used:
        return entities
    out = [e for e in entities if e.id not in used]
    out.extend(merged)
    return out


# Below this pixel area a "solid" blob is an arrowhead/junction dot, not
# hatching worth a structured HatchRegion — same order of magnitude as
# drawing_vectorize's own dot-vs-primitive distinction.
_MIN_HATCH_AREA_PX = 150
_HATCH_SIMPLIFY_EPS = 2.0


def _contour_to_points(contour) -> list[Point] | None:
    import cv2

    approx = cv2.approxPolyDP(contour, _HATCH_SIMPLIFY_EPS, True)
    points = [Point(x=float(p[0][0]), y=float(p[0][1])) for p in approx]
    return points if len(points) >= 3 else None


def _hatch_regions_from_solid(solid_mask) -> list[HatchRegion]:
    """Contour-trace the CV solid-fill mask (Ф4.4): section fills and
    hatching currently ship as opaque raster and are invisible in the DXF
    export (only entities render there) — turning them into HatchRegion
    polygons is what actually gets them into the CAD file.

    Uses ``RETR_CCOMP`` (a 2-level hierarchy: outer boundaries + their
    immediate holes) rather than ``RETR_EXTERNAL`` — a section fill with a
    bolt hole through it (a completely ordinary detail, not an edge case)
    has ink everywhere EXCEPT the hole; ``RETR_EXTERNAL`` used to report the
    hole's own perimeter as ink-covered too, since it only sees the outer
    silhouette and has no notion of nested contours at all."""
    import cv2
    import numpy as np

    if not np.asarray(solid_mask).any():
        return []
    contours, hierarchy = cv2.findContours(
        np.asarray(solid_mask).astype("uint8"), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
    )
    out: list[HatchRegion] = []
    if hierarchy is None:
        return out
    # hierarchy[0][i] = [next, prev, first_child, parent] per OpenCV's
    # RETR_CCOMP convention: parent == -1 is an outer (even-depth) contour.
    hier = hierarchy[0]
    for i, contour in enumerate(contours):
        parent = hier[i][3]
        if parent != -1:
            continue  # a hole contour — collected as a child below, not its own region
        area = cv2.contourArea(contour)
        if area < _MIN_HATCH_AREA_PX:
            continue
        boundary = _contour_to_points(contour)
        if boundary is None:
            continue
        holes: list[list[Point]] = []
        child = hier[i][2]
        while child != -1:
            hole_area = cv2.contourArea(contours[child])
            if hole_area >= _MIN_HATCH_AREA_PX:
                hole_points = _contour_to_points(contours[child])
                if hole_points is not None:
                    holes.append(hole_points)
            child = hier[child][0]  # next sibling at the same (hole) depth
        out.append(HatchRegion(boundary=boundary, holes=holes, pattern="ansi31", confidence=0.6, origin="cv"))
    return out


_HOUGH_DUP_CENTER_TOL_PX = 6.0
_HOUGH_DUP_RADIUS_REL = 0.08


def _hough_circles(ink: Any, existing: list[Entity]) -> list[Circle]:
    """Detect circles directly from the ink via Hough transform — robust to
    the skeleton fragmentation that leaves a drawn circle as a handful of
    disconnected arcs. Circles are fundamental to mechanical drawings and both
    the skeleton tracer and the line-only neural model miss them; this recovers
    them from the raster itself. Deduplicated against circles already found."""
    import cv2
    import numpy as np

    mask = np.asarray(ink)
    if mask.ndim != 2:
        return []
    h, w = mask.shape[:2]
    # On a densely-inked sheet (hatched plans, section fills) HoughCircles
    # hallucinates circles in the texture and the on-ink test can't tell them
    # from real outlines — skip it entirely there.
    if float((mask > 0).mean()) > 0.16:
        return []
    min_dim = min(h, w)
    blurred = cv2.GaussianBlur((mask > 0).astype(np.uint8) * 255, (5, 5), 1.2)
    try:
        found = cv2.HoughCircles(
            blurred, cv2.HOUGH_GRADIENT, dp=1.0,
            minDist=max(16.0, min_dim * 0.04),
            param1=140, param2=52,   # strong accumulator peaks only
            minRadius=max(6, int(min_dim * 0.008)),
            maxRadius=int(min_dim * 0.45),
        )
    except cv2.error:
        return []
    if found is None:
        return []

    have = [
        (e.center.x, e.center.y, e.radius)
        for e in existing
        if isinstance(e, (Circle, Arc))
    ]
    out: list[Circle] = []
    for cx, cy, r in np.round(found[0]).astype(float):
        if len(out) >= 20:  # a real sheet doesn't have dozens of Hough circles
            break
        if any(
            math.hypot(cx - ex, cy - ey) <= _HOUGH_DUP_CENTER_TOL_PX
            and abs(r - er) <= _HOUGH_DUP_RADIUS_REL * max(r, er)
            for ex, ey, er in have
        ):
            continue
        # A genuine circle outline: most on-circle samples are inked, AND the
        # ring is a stroke, not the edge of a solid inked blob — points a few
        # px INSIDE the radius should be mostly clear (a phantom circle inside
        # hatching fails this).
        samples = 32
        on, inside = 0, 0
        for k in range(samples):
            a = 2 * math.pi * k / samples
            ca, sa = math.cos(a), math.sin(a)
            px, py = int(round(cx + r * ca)), int(round(cy + r * sa))
            if 0 <= px < w and 0 <= py < h:
                y0, y1 = max(0, py - 2), min(h, py + 3)
                x0, x1 = max(0, px - 2), min(w, px + 3)
                if (mask[y0:y1, x0:x1] > 0).any():
                    on += 1
            ipx, ipy = int(round(cx + 0.8 * r * ca)), int(round(cy + 0.8 * r * sa))
            if 0 <= ipx < w and 0 <= ipy < h and mask[ipy, ipx] > 0:
                inside += 1
        if on / samples >= 0.85 and inside / samples <= 0.5:
            out.append(Circle(
                center=Point(x=float(cx), y=float(cy)), radius=float(r),
                line_class="contour", width_class="main", origin="cv", confidence=0.8,
            ))
            have.append((cx, cy, r))
    return out


class CvRecognizer:
    name = "cv"

    def recognize(
        self,
        ink: Any,
        exclusion_boxes: list[tuple[int, int, int, int]] | None = None,
    ) -> RecognizeOutput | None:
        result = extract_primitives(ink, exclusion_boxes)
        if result is None:
            return None
        entities: list[Entity] = []
        dots = 0
        for prim in result.primitives:
            entity = _to_entity(prim)
            if entity is None:
                dots += 1
                continue
            entities.append(entity)
        entities = _merge_cocircular_arcs(entities)
        hough = _hough_circles(ink, entities)
        entities.extend(hough)
        hatches = _hatch_regions_from_solid(result.solid_mask)
        entities.extend(hatches)
        logger.info(
            "cv_recognize",
            entities=len(entities),
            dots_skipped=dots,
            hatches=len(hatches),
            thin_px=result.thin_px,
            thick_px=result.thick_px,
        )
        return RecognizeOutput(
            entities=entities,
            keep_raster=result.keep_raster,
            thin_px=result.thin_px,
            thick_px=result.thick_px,
            notes={"dots_skipped": dots},
        )
