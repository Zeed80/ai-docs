"""Topology repair of recognized geometry: fragment consolidation.

Both recognition backends fragment heavily (B0 diagnosis, 2026-07-12): the
patch-based neural model emits one drawn line as dozens of collinear 64px
pieces (1918 fragments on a sheet with ~200 real strokes), and the CV
skeleton tracer splits long strokes at every junction/tick (median segment
6-14px across the golden corpus). Fragmented output is unusable as CAD even
when its pixel coverage is high — and the fragmentation guard in
``verify.arbitrate_recognition`` then rejects the higher-recall proposal
precisely because of the inflated entity count.

This module consolidates a recognized entity list without consulting any
model: merge collinear near-touching segment runs into single long segments,
then re-fit connected chains of short segments that turn consistently into
circles/arcs. Purely geometric, deterministic, conservative — tolerances are
a few pixels, so dashed/dash-dot patterns (gaps of 10px+) survive as separate
strokes, and anything that fails a fit stays exactly as proposed. The
independent coverage verifier re-scores the consolidated result afterwards,
so a bad merge cannot ship unnoticed.
"""

from __future__ import annotations

import math
from typing import Any

import structlog

from app.ai.cad_ir.schema import Arc, Circle, Entity, Point, Segment
from app.ai.drawing_vectorize import _fit_circle_or_arc

logger = structlog.get_logger()

# Collinear merge: two segments join when their directions differ by no more
# than _ANGLE_TOL_DEG, both lie within _OFFSET_TOL_PX of the shared infinite
# line, and the gap between their projections is at most _GAP_TOL_PX.
# _GAP_TOL_PX is deliberately below any ЕСКД dash gap so line-type patterns
# (штриховая/штрихпунктирная) are not welded solid.
_ANGLE_TOL_DEG = 3.0
_OFFSET_TOL_PX = 2.5
_GAP_TOL_PX = 6.0
_GRID_CELL_PX = 48
# Post-merge specks: a merged run shorter than this is recognition noise.
_MIN_SEGMENT_LEN_PX = 2.0

# Arc re-fit: chains of short segments whose direction turns consistently.
_CHAIN_ENDPOINT_SNAP_PX = 3.0
_CHAIN_MIN_SEGMENTS = 4
_CHAIN_MAX_SEGMENT_LEN_PX = 60.0
_CHAIN_MIN_TOTAL_TURN_DEG = 50.0
# The fitted circle must pass near edge midpoints too, not only vertices —
# a rectangle's corners sit exactly on its circumcircle, its edge midpoints
# do not. This is what separates "polygonal circle approximation" from
# "actual polygonal contour".
_CHAIN_MIDPOINT_DEV_CAP_PX = 3.5


def _seg_len(seg: Segment) -> float:
    return math.hypot(seg.p2.x - seg.p1.x, seg.p2.y - seg.p1.y)


def _angle_deg(seg: Segment) -> float:
    return math.degrees(math.atan2(seg.p2.y - seg.p1.y, seg.p2.x - seg.p1.x)) % 180.0


def _angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[rj] = ri


def _collinear_mergeable(a: Segment, b: Segment, gap_tol: float = _GAP_TOL_PX) -> bool:
    if _angle_diff(_angle_deg(a), _angle_deg(b)) > _ANGLE_TOL_DEG:
        return False
    # Shared line = the longer segment's line; both ends of the shorter one
    # must lie within the offset tolerance of it.
    long_seg, short_seg = (a, b) if _seg_len(a) >= _seg_len(b) else (b, a)
    dx, dy = long_seg.p2.x - long_seg.p1.x, long_seg.p2.y - long_seg.p1.y
    norm = math.hypot(dx, dy)
    if norm < 1e-6:
        return False
    ux, uy = dx / norm, dy / norm
    nx, ny = -uy, ux
    for p in (short_seg.p1, short_seg.p2):
        off = abs((p.x - long_seg.p1.x) * nx + (p.y - long_seg.p1.y) * ny)
        if off > _OFFSET_TOL_PX:
            return False
    # Projection intervals on the shared direction: overlap or a small gap.
    ta = sorted(((a.p1.x * ux + a.p1.y * uy), (a.p2.x * ux + a.p2.y * uy)))
    tb = sorted(((b.p1.x * ux + b.p1.y * uy), (b.p2.x * ux + b.p2.y * uy)))
    gap = max(ta[0], tb[0]) - min(ta[1], tb[1])
    return gap <= gap_tol


def _merge_group(group: list[Segment], line_class: str | None = None) -> Segment:
    """One consolidated segment from a run of collinear fragments: principal
    direction from the length-weighted covariance, extent from the extreme
    endpoint projections. ``line_class`` overrides the anchor's class when
    the run was recognized as a dash pattern (hidden/axis)."""
    total = sum(_seg_len(s) for s in group) or 1.0
    # Length-weighted mean direction (angles are mod 180: use doubled-angle
    # averaging so 179deg and 1deg average to 0deg, not 90deg).
    sx = sum(math.cos(2 * math.radians(_angle_deg(s))) * _seg_len(s) for s in group)
    sy = sum(math.sin(2 * math.radians(_angle_deg(s))) * _seg_len(s) for s in group)
    theta = math.atan2(sy, sx) / 2.0
    ux, uy = math.cos(theta), math.sin(theta)
    # Length-weighted centroid anchors the line's offset.
    cx = sum((s.p1.x + s.p2.x) / 2 * _seg_len(s) for s in group) / total
    cy = sum((s.p1.y + s.p2.y) / 2 * _seg_len(s) for s in group) / total
    ts: list[float] = []
    for s in group:
        for p in (s.p1, s.p2):
            ts.append((p.x - cx) * ux + (p.y - cy) * uy)
    t0, t1 = min(ts), max(ts)
    anchor = max(group, key=_seg_len)
    confidence = sum(s.confidence * _seg_len(s) for s in group) / total
    return Segment(
        p1=Point(x=cx + ux * t0, y=cy + uy * t0),
        p2=Point(x=cx + ux * t1, y=cy + uy * t1),
        line_class=line_class or anchor.line_class,
        width_class="thin" if line_class else anchor.width_class,
        confidence=round(min(1.0, confidence), 4),
        origin=anchor.origin,
    )


def _collinear_groups(
    segments: list[Segment], gap_tol: float
) -> list[list[Segment]]:
    """Union-find over locally close, collinear segments. Candidate pairs
    come from a coarse spatial hash of padded segment AABBs, so the pass
    stays near-linear on the thousands of fragments the neural backend
    emits."""
    n = len(segments)
    if n < 2:
        return [[s] for s in segments]
    grid: dict[tuple[int, int], list[int]] = {}
    for i, s in enumerate(segments):
        x0 = min(s.p1.x, s.p2.x) - gap_tol
        x1 = max(s.p1.x, s.p2.x) + gap_tol
        y0 = min(s.p1.y, s.p2.y) - gap_tol
        y1 = max(s.p1.y, s.p2.y) + gap_tol
        for gx in range(int(x0 // _GRID_CELL_PX), int(x1 // _GRID_CELL_PX) + 1):
            for gy in range(int(y0 // _GRID_CELL_PX), int(y1 // _GRID_CELL_PX) + 1):
                grid.setdefault((gx, gy), []).append(i)

    uf = _UnionFind(n)
    checked: set[tuple[int, int]] = set()
    for bucket in grid.values():
        for ai in range(len(bucket)):
            for bi in range(ai + 1, len(bucket)):
                pair = (bucket[ai], bucket[bi])
                if pair in checked:
                    continue
                checked.add(pair)
                if uf.find(pair[0]) == uf.find(pair[1]):
                    continue
                if _collinear_mergeable(segments[pair[0]], segments[pair[1]], gap_tol):
                    uf.union(pair[0], pair[1])

    groups: dict[int, list[Segment]] = {}
    for i, s in enumerate(segments):
        groups.setdefault(uf.find(i), []).append(s)
    return list(groups.values())


def _merge_collinear_segments(segments: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for group in _collinear_groups(segments, _GAP_TOL_PX):
        merged = group[0] if len(group) == 1 else _merge_group(group)
        if _seg_len(merged) >= _MIN_SEGMENT_LEN_PX:
            out.append(merged)
    return out


# ── Dash-pattern recognition (B2: line-type semantics) ──────────────────────
#
# ЕСКД line types are drawn as dash patterns: штриховая (hidden contour,
# dashes 2-8mm with 1-2mm gaps) and штрихпунктирная (axis/center, long
# 5-30mm dashes alternating with dots/short dashes, 3-5mm gaps). After
# fragment consolidation those survive as runs of separate collinear
# segments — deliberately, the 6px weld tolerance must not fuse them. This
# second pass RECOGNIZES the runs by their regularity and merges each into
# ONE segment with the correct line_class, which is the canonical CAD
# representation (renderers/DXF layers draw the pattern themselves).
_DASH_GAP_MAX_PX = 22.0  # 3-5mm gaps at the typical 4px/mm working scale
_DASH_ELEMENT_MAX_PX = 140.0  # a 30mm+ stroke is real contour, not a dash
_DASH_MIN_ELEMENTS = 4
_AXIS_MIN_ELEMENTS = 3
# A dash-dot short element is decisively shorter than the long strokes.
_AXIS_SHORT_RATIO = 0.4


def _classify_dash_group(group: list[Segment]) -> str | None:
    """hidden / axis / None for a collinear run, judged purely on the
    regularity of element lengths and gaps — no thresholds on ink."""
    if len(group) < _AXIS_MIN_ELEMENTS:
        return None
    # Order along the shared direction.
    anchor = max(group, key=_seg_len)
    dx, dy = anchor.p2.x - anchor.p1.x, anchor.p2.y - anchor.p1.y
    norm = math.hypot(dx, dy) or 1.0
    ux, uy = dx / norm, dy / norm
    spans = sorted(
        (
            sorted((s.p1.x * ux + s.p1.y * uy, s.p2.x * ux + s.p2.y * uy)),
            _seg_len(s),
        )
        for s in group
    )
    lens = [length for _span, length in spans]
    gaps = [
        max(0.0, spans[i + 1][0][0] - spans[i][0][1])
        for i in range(len(spans) - 1)
    ]
    if not gaps or max(lens) > _DASH_ELEMENT_MAX_PX:
        return None
    # Gaps must be regular — one huge gap means two unrelated strokes that
    # happen to share a line (a wall broken by a doorway), not a pattern.
    med_gap = sorted(gaps)[len(gaps) // 2]
    if med_gap <= 0 or max(gaps) > max(3.0 * med_gap, 12.0):
        return None

    long_thr = _AXIS_SHORT_RATIO * max(lens)
    shorts = [length for length in lens if length <= long_thr]
    longs = [length for length in lens if length > long_thr]
    if shorts and longs:
        # Axis (штрихпунктирная): long strokes alternating with short
        # dashes/dots — no two shorts in a row, at least two longs.
        kinds = ["s" if length <= long_thr else "l" for _span, length in spans]
        if (
            len(longs) >= 2
            and "ss" not in "".join(kinds)
            and kinds[0] == "l"
            and kinds[-1] == "l"
        ):
            return "axis"
        return None
    if len(group) >= _DASH_MIN_ELEMENTS:
        # Hidden (штриховая): uniform dashes.
        if max(lens) <= 2.5 * min(lens):
            return "hidden"
    return None


def _recognize_dash_patterns(
    segments: list[Segment],
) -> tuple[list[Segment], int]:
    """Merge recognized dash runs into single hidden/axis segments; leave
    everything else untouched. Only short elements participate — long
    contour strokes can never be swallowed by this pass."""
    candidates = [s for s in segments if _seg_len(s) <= _DASH_ELEMENT_MAX_PX]
    rest = [s for s in segments if _seg_len(s) > _DASH_ELEMENT_MAX_PX]
    if len(candidates) < _AXIS_MIN_ELEMENTS:
        return segments, 0
    out: list[Segment] = list(rest)
    recognized = 0
    for group in _collinear_groups(candidates, _DASH_GAP_MAX_PX):
        line_class = _classify_dash_group(group) if len(group) > 1 else None
        if line_class is None:
            out.extend(group)
            continue
        out.append(_merge_group(group, line_class=line_class))
        recognized += 1
    return out, recognized


def _snap_key(p: Point) -> tuple[int, int]:
    return (round(p.x / _CHAIN_ENDPOINT_SNAP_PX), round(p.y / _CHAIN_ENDPOINT_SNAP_PX))


def _extract_chains(segments: list[Segment]) -> list[tuple[list[Segment], list[Point], bool]]:
    """Connected runs of short segments joined end-to-end (both endpoints of
    every interior node used exactly twice). Returns (members, ordered
    vertices, closed) per chain."""
    short = [s for s in segments if _seg_len(s) <= _CHAIN_MAX_SEGMENT_LEN_PX]
    adj: dict[tuple[int, int], list[int]] = {}
    for i, s in enumerate(short):
        adj.setdefault(_snap_key(s.p1), []).append(i)
        adj.setdefault(_snap_key(s.p2), []).append(i)

    visited: set[int] = set()
    chains: list[tuple[list[Segment], list[Point], bool]] = []

    def _other_end(seg: Segment, key: tuple[int, int]) -> Point:
        return seg.p2 if _snap_key(seg.p1) == key else seg.p1

    for start_idx, start_seg in enumerate(short):
        if start_idx in visited:
            continue
        members = [start_seg]
        visited.add(start_idx)
        pts = [start_seg.p1, start_seg.p2]
        closed = False
        # Grow in both directions while the junction degree is exactly 2.
        for end in (0, 1):
            while True:
                key = _snap_key(pts[-1] if end else pts[0])
                candidates = [j for j in adj.get(key, []) if j not in visited]
                if len(adj.get(key, [])) != 2 or len(candidates) != 1:
                    break
                j = candidates[0]
                visited.add(j)
                members.append(short[j])
                nxt = _other_end(short[j], key)
                if end:
                    pts.append(nxt)
                else:
                    pts.insert(0, nxt)
                if _snap_key(pts[0]) == _snap_key(pts[-1]) and len(members) >= 3:
                    closed = True
                    break
            if closed:
                break
        if len(members) >= _CHAIN_MIN_SEGMENTS:
            chains.append((members, pts, closed))
    return chains


def _total_turn_deg(pts: list[Point]) -> float:
    total = 0.0
    for i in range(1, len(pts) - 1):
        a1 = math.atan2(pts[i].y - pts[i - 1].y, pts[i].x - pts[i - 1].x)
        a2 = math.atan2(pts[i + 1].y - pts[i].y, pts[i + 1].x - pts[i].x)
        d = math.degrees(a2 - a1)
        while d > 180.0:
            d -= 360.0
        while d < -180.0:
            d += 360.0
        total += d
    return abs(total)


def _refit_chain(members: list[Segment], pts: list[Point], closed: bool) -> Entity | None:
    """Kåsa circle fit over chain vertices, accepted only when edge midpoints
    also sit on the circle (rejects genuine polygonal contours)."""
    import numpy as np

    if _total_turn_deg(pts) < _CHAIN_MIN_TOTAL_TURN_DEG:
        return None
    ptsf = np.array([[p.x, p.y] for p in pts], dtype=np.float32)
    prim = _fit_circle_or_arc(ptsf, closed)
    if prim is None or prim.center is None or prim.radius is None:
        return None
    for i in range(len(pts) - 1):
        mx, my = (pts[i].x + pts[i + 1].x) / 2, (pts[i].y + pts[i + 1].y) / 2
        dev = abs(math.hypot(mx - prim.center[0], my - prim.center[1]) - prim.radius)
        if dev > _CHAIN_MIDPOINT_DEV_CAP_PX:
            return None
    anchor = max(members, key=_seg_len)
    common = {
        "line_class": anchor.line_class,
        "width_class": anchor.width_class,
        "confidence": round(min(1.0, min(prim.confidence, min(s.confidence for s in members))), 4),
        "origin": anchor.origin,
    }
    if prim.kind == "circle":
        return Circle(center=Point(x=prim.center[0], y=prim.center[1]), radius=prim.radius, **common)
    return Arc(
        center=Point(x=prim.center[0], y=prim.center[1]),
        radius=prim.radius,
        start_angle=prim.start_angle,
        end_angle=prim.end_angle,
        **common,
    )


def _refit_chains_to_arcs(segments: list[Segment]) -> tuple[list[Segment], list[Entity]]:
    chains = _extract_chains(segments)
    if not chains:
        return segments, []
    replaced_ids: set[str] = set()
    fitted: list[Entity] = []
    for members, pts, closed in chains:
        entity = _refit_chain(members, pts, closed)
        if entity is not None:
            fitted.append(entity)
            replaced_ids.update(s.id for s in members)
    if not fitted:
        return segments, []
    return [s for s in segments if s.id not in replaced_ids], fitted


# Co-circular arc merge: skeleton junctions and patch borders shred one drawn
# circle into arc fragments. Fragments sharing a center/radius whose angular
# gaps are small (measured in arc-length pixels, same tolerance as the
# collinear merge) are one stroke.
_ARC_CENTER_TOL_PX = 5.0
_ARC_RADIUS_REL_TOL = 0.06
_FULL_CIRCLE_MIN_SPAN_DEG = 330.0


def _norm_interval(a: Arc) -> tuple[float, float]:
    start, end = a.start_angle % 360.0, a.end_angle % 360.0
    if end < start:
        end += 360.0
    return start, end


def _merge_cocircular_arcs(arcs: list[Arc]) -> tuple[list[Arc], list[Entity]]:
    """Merge arcs on the same circle into longer arcs / a full circle.

    Returns (untouched arcs, merged replacements)."""
    if len(arcs) < 2:
        return arcs, []
    uf = _UnionFind(len(arcs))
    for i in range(len(arcs)):
        for j in range(i + 1, len(arcs)):
            a, b = arcs[i], arcs[j]
            dc = math.hypot(a.center.x - b.center.x, a.center.y - b.center.y)
            if dc <= _ARC_CENTER_TOL_PX and abs(a.radius - b.radius) <= _ARC_RADIUS_REL_TOL * max(a.radius, b.radius):
                uf.union(i, j)
    groups: dict[int, list[Arc]] = {}
    for i, a in enumerate(arcs):
        groups.setdefault(uf.find(i), []).append(a)

    kept: list[Arc] = []
    merged: list[Entity] = []
    for group in groups.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        cx = sum(a.center.x for a in group) / len(group)
        cy = sum(a.center.y for a in group) / len(group)
        r = sum(a.radius for a in group) / len(group)
        gap_deg = math.degrees(_GAP_TOL_PX / max(r, 1.0))
        intervals = sorted(_norm_interval(a) for a in group)
        runs: list[list[float]] = [list(intervals[0])]
        for start, end in intervals[1:]:
            if start - runs[-1][1] <= gap_deg:
                runs[-1][1] = max(runs[-1][1], end)
            else:
                runs.append([start, end])
        # wraparound: the last run may connect to the first through 360
        if len(runs) > 1 and (runs[0][0] + 360.0) - runs[-1][1] <= gap_deg:
            runs[0][0] = runs[-1][0] - 360.0
            runs.pop()
        anchor = max(group, key=lambda a: abs(_norm_interval(a)[1] - _norm_interval(a)[0]))
        common = {
            "line_class": anchor.line_class,
            "width_class": anchor.width_class,
            "confidence": round(min(a.confidence for a in group), 4),
            "origin": anchor.origin,
        }
        total_span = sum(end - start for start, end in runs)
        if total_span >= _FULL_CIRCLE_MIN_SPAN_DEG:
            merged.append(Circle(center=Point(x=cx, y=cy), radius=r, **common))
            continue
        made_any = False
        for start, end in runs:
            if end - start <= 0.5:
                continue
            made_any = True
            merged.append(Arc(
                center=Point(x=cx, y=cy), radius=r,
                start_angle=start, end_angle=end, **common,
            ))
        if not made_any:
            kept.extend(group)
    return kept, merged


def consolidate_entities(entities: list[Entity]) -> tuple[list[Entity], dict[str, Any]]:
    """Fragment consolidation over a recognized entity list.

    Segments are merged along shared lines and short chains re-fitted into
    circles/arcs; every other entity type passes through untouched. Returns
    the consolidated list plus stats for the arbitration notes/audit trail.
    """
    segments = [e for e in entities if isinstance(e, Segment)]
    arcs = [e for e in entities if isinstance(e, Arc)]
    others = [e for e in entities if not isinstance(e, (Segment, Arc))]
    if len(segments) < 2 and len(arcs) < 2:
        return entities, {"consolidated": False}

    merged = _merge_collinear_segments(segments)
    kept, fitted = _refit_chains_to_arcs(merged)
    kept, dash_lines = _recognize_dash_patterns(kept)
    all_arcs = arcs + [e for e in fitted if isinstance(e, Arc)]
    fitted_non_arc = [e for e in fitted if not isinstance(e, Arc)]
    kept_arcs, merged_arcs = _merge_cocircular_arcs(all_arcs)
    stats = {
        "consolidated": True,
        "segments_in": len(segments),
        "segments_out": len(kept),
        "arcs_fitted": len(fitted),
        "arcs_in": len(arcs),
        "arcs_merged": len(merged_arcs),
        "dash_lines": dash_lines,
    }
    if len(kept) + len(fitted) < len(segments) or merged_arcs:
        logger.info("cad_topology_consolidated", **stats)
    return [*others, *kept, *fitted_non_arc, *kept_arcs, *merged_arcs], stats
