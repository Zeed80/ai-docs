"""Vector reconstruction of a diffusion-cleaned technical drawing: rebuild
every stroke from its skeleton so lines come out mathematically straight,
uniformly thin, and snapped to ЕСКД canonical angles — the geometric quality
diffusion cannot promise on its own.

Why this exists when drawing_cleanup._snap_canonical_lines already tried line
straightening and had to ship disabled: that approach drew corrected lines ON
TOP of the existing raster from Hough fragments, and every one of its live
corruption rounds traces back to that shape — near-duplicate Hough fragments
each redrawn into a fatter bar, unrelated segments bridged, text read as
"lines" and clobbered. This module inverts the design:

- REPLACEMENT, not overpainting: the output canvas starts white and every
  stroke is drawn exactly once from its skeleton path. Nothing is layered on
  top of old raster, so duplicates cannot compound.
- One path per stroke: the skeleton (1px medial axis) yields a single
  traced path per stroke where Hough yields many overlapping fragments.
- Local width: stroke thickness comes from the distance transform sampled
  along the stroke's own skeleton — it cannot be fooled by a dense table
  grid nearby the way a perpendicular probe was.
- Curves are first-class: a path that doesn't fit a straight line is fitted
  as a circle/arc, else redrawn as its own (lightly simplified) polyline —
  geometry is preserved 1:1 instead of being left as mixed-in raster.
- Filled shapes (dimension arrowheads, section fills, welding symbols) are
  detected via the distance transform and kept as original raster — a
  skeleton would collapse them into wrong thin "Y" strokes.
- Self-verification: before returning, the redrawn ink is compared against
  the original (coverage recall AND precision within a small dilation). If
  either is off, the function DECLINES (returns None) and the caller falls
  back to plain binarization — a corrupted redraw cannot ship silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()

_CANONICAL_ANGLES_DEG = (0.0, 45.0, 90.0, 135.0)
_CANONICAL_TOLERANCE_DEG = 4.0

_MAX_INK_FRACTION = 0.30  # denser than this = not a line drawing; decline
# Straight-line fit acceptance: max perpendicular deviation of skeleton points
# from the fitted line. Scales mildly with length (long diffusion lines wobble
# by a few px) but is capped — an "arc" flat enough to pass the cap is
# indistinguishable from a straight line at sheet scale anyway.
_STRAIGHT_DEV_CAP_PX = 6.0
_CIRCLE_DEV_CAP_PX = 3.0
_MIN_CIRCLE_RADIUS_PX = 4.0
_POLYLINE_SIMPLIFY_EPS = 1.2  # px; removes pixel jitter, keeps curve shape
_JUNCTION_SNAP_RADIUS_PX = 4.0

# Self-verification: how far a redrawn stroke may sit from the original ink
# (and vice versa) before the redraw is declared corrupted and declined.
# Straightening displaces a wobbly line by at most the straight-fit cap. The
# tolerance scales with sheet size (see _coverage_dilate_px): a fixed 5px is
# fine on a 2000px sheet but lets real corruption through on a small dense
# one, where 5px spans a whole hatching period.
_COVERAGE_DILATE_MAX_PX = 5
_MIN_COVERAGE_RECALL = 0.85
_MIN_COVERAGE_PRECISION = 0.85


def _coverage_dilate_px(h: int, w: int) -> int:
    # Floor of 3: legitimate straightening may displace a wobbly stroke by a
    # few px even on a small sheet — the tolerance must never be tighter than
    # the displacement the redraw itself is allowed to introduce.
    return max(3, min(_COVERAGE_DILATE_MAX_PX, round(math.hypot(w, h) * 0.004)))


@dataclass
class RawPrimitive:
    """One fitted stroke primitive in source-pixel coordinates.

    ``confidence`` derives from the fit residual (1.0 = points sit exactly on
    the primitive; the acceptance cap maps to ~0.5). Polyline fallbacks carry
    the lowest confidence — they mean "no clean primitive matched".
    """

    kind: str  # "segment" | "circle" | "arc" | "polyline" | "dot"
    thickness_px: int
    is_thick: bool
    confidence: float
    # segment endpoints / dot center
    p1: tuple[float, float] | None = None
    p2: tuple[float, float] | None = None
    # circle/arc
    center: tuple[float, float] | None = None
    radius: float | None = None
    start_angle: float | None = None
    end_angle: float | None = None
    # polyline (simplified vertices)
    points: list[tuple[float, float]] = field(default_factory=list)
    closed: bool = False


@dataclass
class ExtractResult:
    """Primitives plus everything needed to re-render/verify the sheet."""

    primitives: list[RawPrimitive]
    keep_raster: "object"  # bool HxW mask: solid fills + exclusion regions
    # Solid-fill component of keep_raster ALONE (arrowheads, section fills,
    # weld symbols — density-detected, not caller-supplied exclusion boxes).
    # Exposed separately so callers can turn genuine hatching/fills into
    # structured HatchRegion entities (Ф4.4) without also contour-tracing
    # excluded TEXT regions, which are unrelated raster passthrough.
    solid_mask: "object"
    thin_px: int
    thick_px: int
    width: int
    height: int


def extract_primitives(
    ink, exclusion_boxes: list[tuple[int, int, int, int]] | None = None
) -> ExtractResult | None:
    """Fit vector primitives to ``ink`` (uint8 mask, 255 = ink).

    Returns ``None`` when the sheet is not a line drawing (density gate) or
    yields no traceable strokes — callers must treat that as "decline".
    """
    import cv2
    import numpy as np

    h, w = ink.shape[:2]
    ink_bool = ink > 0
    frac = float(ink_bool.mean())
    if frac == 0.0 or frac > _MAX_INK_FRACTION:
        logger.info("vectorize_declined_ink_fraction", fraction=round(frac, 3))
        return None

    dist = cv2.distanceTransform(ink_bool.astype(np.uint8), cv2.DIST_L2, 3)

    skel_full = _skeletonize(ink_bool)
    if not skel_full.any():
        logger.info("vectorize_declined_empty_skeleton")
        return None
    median_radius = float(np.median(dist[skel_full])) or 1.0

    solid = _solid_regions(ink_bool, dist, median_radius)

    excl = np.zeros((h, w), dtype=bool)
    for x0, y0, x1, y1 in exclusion_boxes or []:
        excl[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True

    keep_raster = ink_bool & (solid | excl)
    skel = skel_full & ~solid & ~excl

    paths, junction_points = _trace_paths(skel)
    if not paths:
        logger.info("vectorize_declined_no_paths")
        return None

    widths = [
        2.0 * float(np.median(dist[pts[:, 1], pts[:, 0]])) for pts in paths
    ]
    thin_px, thick_px, split = _width_classes(widths, [len(p) for p in paths])

    primitives = []
    for pts, width in zip(paths, widths):
        is_thick = split is not None and width >= split
        thickness = thick_px if is_thick else thin_px
        primitives.extend(_fit_path(pts, thickness, is_thick, junction_points))

    return ExtractResult(
        primitives=primitives,
        keep_raster=keep_raster,
        solid_mask=solid,
        thin_px=thin_px,
        thick_px=thick_px,
        width=w,
        height=h,
    )


def render_primitives(result: ExtractResult):
    """Deterministic raster of an ``ExtractResult``: white canvas, kept raster
    regions copied through, every primitive drawn exactly once."""
    import numpy as np

    canvas = np.full((result.height, result.width), 255, dtype=np.uint8)
    canvas[result.keep_raster] = 0
    for prim in result.primitives:
        _draw_primitive(canvas, prim)
    return canvas


def redraw_ink(ink, exclusion_boxes: list[tuple[int, int, int, int]] | None = None):
    """Rebuild the drawing from ``ink`` (uint8 mask, 255 = ink) as clean
    vector-quality strokes. Returns a grayscale uint8 canvas (255 background,
    anti-aliased dark strokes) or ``None`` when the redraw would be unsafe —
    callers must treat ``None`` as "keep the binarized image as-is".

    ``exclusion_boxes`` (x0, y0, x1, y1) mark regions whose original raster
    ink is copied through untouched (text handled by text_preserve, title
    block) instead of being re-stroked.
    """
    result = extract_primitives(ink, exclusion_boxes)
    if result is None:
        return None
    canvas = render_primitives(result)
    redrawn = canvas < 128
    if not _verify_coverage(ink > 0, redrawn):
        return None
    return canvas


# ── Skeletonization ──────────────────────────────────────────────────────────


def _skeletonize(ink_bool):
    """Medial-axis thinning. Uses cv2.ximgproc (opencv-contrib) when present;
    falls back to a vectorized Zhang-Suen — identical topology, pure numpy.
    Iteration count ≈ max stroke half-width, small for line drawings."""
    import cv2
    import numpy as np

    if hasattr(cv2, "ximgproc"):
        thinned = cv2.ximgproc.thinning(ink_bool.astype(np.uint8) * 255)
        return thinned > 0
    return _zhang_suen(ink_bool)


def _zhang_suen(img):
    import numpy as np

    img = img.astype(bool).copy()

    def _neighbors(padded):
        # Clockwise from north: P2..P9 per the original paper's notation.
        return [
            padded[0:-2, 1:-1],  # P2 N
            padded[0:-2, 2:],    # P3 NE
            padded[1:-1, 2:],    # P4 E
            padded[2:, 2:],      # P5 SE
            padded[2:, 1:-1],    # P6 S
            padded[2:, 0:-2],    # P7 SW
            padded[1:-1, 0:-2],  # P8 W
            padded[0:-2, 0:-2],  # P9 NW
        ]

    while True:
        changed = False
        for phase in (0, 1):
            padded = np.pad(img, 1)
            nb = _neighbors(padded)
            b = sum(n.astype(np.uint8) for n in nb)
            seq = nb + [nb[0]]
            a = sum(((~seq[i]) & seq[i + 1]).astype(np.uint8) for i in range(8))
            if phase == 0:
                c1 = ~(nb[0] & nb[2] & nb[4])  # P2·P4·P6 = 0
                c2 = ~(nb[2] & nb[4] & nb[6])  # P4·P6·P8 = 0
            else:
                c1 = ~(nb[0] & nb[2] & nb[6])  # P2·P4·P8 = 0
                c2 = ~(nb[0] & nb[4] & nb[6])  # P2·P6·P8 = 0
            cond = img & (b >= 2) & (b <= 6) & (a == 1) & c1 & c2
            if cond.any():
                img[cond] = False
                changed = True
        if not changed:
            return img


# ── Filled-region detection ──────────────────────────────────────────────────


def _solid_regions(ink_bool, dist, median_radius: float):
    """Mask of filled areas (arrowheads, section fills, symbols): places whose
    inscribed radius is far beyond a normal stroke's. Kept as raster — their
    skeleton is a meaningless thin spur that would redraw them wrong."""
    import cv2
    import numpy as np

    core = dist > max(3.0, 2.6 * median_radius)
    if not core.any():
        return np.zeros_like(ink_bool)
    margin = int(2 * median_radius) + 3
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * margin + 1, 2 * margin + 1))
    grown = cv2.dilate(core.astype(np.uint8), kernel) > 0
    return grown & ink_bool


# ── Skeleton → ordered paths ─────────────────────────────────────────────────


def _trace_paths(skel):
    """Split the skeleton at junction pixels and order each remaining simple
    arc into a point sequence. Junction pixels themselves are clustered; each
    adjoining path gets the cluster centroid appended at that end so redrawn
    strokes meet exactly where the originals met.

    Returns (paths, junction_points): paths as Nx2 int arrays of (x, y),
    junction_points as an Mx2 float array of cluster centroids (possibly
    empty)."""
    import cv2
    import numpy as np

    skel_u8 = skel.astype(np.uint8)
    # Crossing number: junctions are pixels with ≥3 distinct 0→1 transitions
    # around their 8-neighborhood. A raw neighbor count (≥3) falsely marks
    # staircase corners of discretized circles/diagonals as junctions and
    # shreds one drawn circle into dozens of chord fragments.
    padded = np.pad(skel.astype(bool), 1)
    nb = [
        padded[0:-2, 1:-1],  # N
        padded[0:-2, 2:],    # NE
        padded[1:-1, 2:],    # E
        padded[2:, 2:],      # SE
        padded[2:, 1:-1],    # S
        padded[2:, 0:-2],    # SW
        padded[1:-1, 0:-2],  # W
        padded[0:-2, 0:-2],  # NW
    ]
    seq = nb + [nb[0]]
    transitions = sum(((~seq[i]) & seq[i + 1]).astype(np.uint8) for i in range(8))
    junctions = skel & (transitions >= 3)

    # Junction clusters → centroids, and per-pixel lookup of its centroid.
    junction_points = np.empty((0, 2), dtype=np.float32)
    junction_of: dict[tuple[int, int], tuple[float, float]] = {}
    if junctions.any():
        n_j, j_labels, _stats, j_centroids = cv2.connectedComponentsWithStats(
            junctions.astype(np.uint8), connectivity=8
        )
        junction_points = j_centroids[1:].astype(np.float32)
        jys, jxs = np.nonzero(junctions)
        for x, y in zip(jxs, jys):
            cx, cy = j_centroids[j_labels[y, x]]
            junction_of[(int(x), int(y))] = (float(cx), float(cy))

    simple = skel & ~junctions
    n, labels = cv2.connectedComponents(simple.astype(np.uint8), connectivity=8)
    ys, xs = np.nonzero(simple)
    by_label: dict[int, list[tuple[int, int]]] = {}
    for x, y in zip(xs.tolist(), ys.tolist()):
        by_label.setdefault(int(labels[y, x]), []).append((x, y))

    paths = []
    for pts in by_label.values():
        for ordered in _order_path_chains(pts):
            if not ordered:
                continue
            # Re-attach junction centroids so strokes meet where originals met.
            for end_idx, insert_front in ((0, True), (-1, False)):
                ex, ey = ordered[end_idx]
                attached = None
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        attached = junction_of.get((ex + dx, ey + dy)) or attached
                if attached is not None:
                    pt = (int(round(attached[0])), int(round(attached[1])))
                    if insert_front:
                        ordered.insert(0, pt)
                    else:
                        ordered.append(pt)
            paths.append(np.array(ordered, dtype=np.int32))
    return paths, junction_points


def _order_path_chains(points: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """Order a skeleton component's pixels into walkable sequences, covering
    every pixel.

    A single greedy walk only holds when the component really is one simple
    arc. Splitting at crossing-number junctions does not guarantee that: a
    glyph, a dashed-line end, an arrowhead or a tangential meeting leaves
    branches behind, and a walk that stops at the first dead end abandons
    them. Measured on a dense production sheet, one walk per component
    returned 39 368 of 97 098 skeleton pixels — the abandoned 60% were the
    sheet's thin lines (extension, dimension, centre), so the reconstruction
    silently lost half the drawing while reporting nothing wrong.

    So walk repeatedly until the component is exhausted. Free ends are used
    as starts first (a walk from a true endpoint follows a whole branch);
    whatever remains is cyclic and is started anywhere.
    """
    pset = set(points)
    chains: list[list[tuple[int, int]]] = []

    def _nbrs(p):
        x, y = p
        return [
            (x + dx, y + dy)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if (dx or dy) and (x + dx, y + dy) in pset
        ]

    # Degrees in the untouched component: recomputing them after every walk
    # would be quadratic, and a stale "free end" only costs a start position.
    ends = [p for p in points if len(_nbrs(p)) <= 1]
    starts = iter(ends + points)

    while pset:
        start = None
        for candidate in starts:
            if candidate in pset:
                start = candidate
                break
        if start is None:  # every listed start consumed; take any leftover
            start = next(iter(pset))
        ordered = [start]
        pset.discard(start)
        cur = start
        while True:
            candidates = _nbrs(cur)
            if not candidates:
                break
            # Prefer the 4-connected continuation to avoid diagonal shortcuts.
            cur = min(candidates, key=lambda q: abs(q[0] - cur[0]) + abs(q[1] - cur[1]))
            ordered.append(cur)
            pset.discard(cur)
        chains.append(ordered)
    return chains


def _order_path(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The longest single walk through a component (kept for callers that
    want one representative stroke rather than full coverage)."""
    chains = _order_path_chains(points)
    return max(chains, key=len) if chains else []


# ── Stroke width classes ─────────────────────────────────────────────────────


def _width_classes(widths: list[float], lengths: list[int]):
    """ЕСКД uses two line weights: основная (contour) and тонкая (dimensions,
    hatching, extension lines). Cluster measured widths — length-weighted so a
    long contour counts more than a stray fragment — into those two classes.
    Returns (thin_px, thick_px, split): render thickness for each class and
    the width threshold between them, or split=None when the sheet only
    really has one weight."""
    import numpy as np

    caps = np.minimum(np.array(lengths), 1000)
    weighted = np.repeat(np.array(widths, dtype=np.float32), caps)
    median = float(np.median(weighted))
    thin = weighted[weighted <= median]
    thick = weighted[weighted > median]
    if len(thin) == 0 or len(thick) == 0:
        t = max(1, round(median))
        return t, t, None
    thin_c, thick_c = float(np.median(thin)), float(np.median(thick))
    if thick_c < 1.4 * thin_c:
        t = max(1, round(median))
        return t, t, None
    thin_px = max(1, round(thin_c))
    thick_px = max(thin_px + 1, round(thick_c))
    return thin_px, thick_px, (thin_c + thick_c) / 2.0


# ── Primitive fitting + drawing ──────────────────────────────────────────────


def _fit_confidence(dev: float, cap: float) -> float:
    """Residual → confidence: exact fit = 1.0, at the acceptance cap = 0.5."""
    if cap <= 0:
        return 0.5
    return max(0.5, 1.0 - 0.5 * (dev / cap))


_CORNER_SPLIT_MIN_EDGE_PX = 15.0


def _fit_path(pts, thickness: int, is_thick: bool, junction_points) -> list[RawPrimitive]:
    """Fit one ordered skeleton path to primitives (straight segment →
    circle/arc → corner-split segment chain → simplified polyline fallback),
    preserving the exact same acceptance rules the renderer used when it drew
    directly. A path may yield several primitives: an L-shaped stroke whose
    corner is not a topological junction splits into its straight edges."""
    import cv2
    import numpy as np

    n = len(pts)
    if n == 1:
        return [RawPrimitive(
            kind="dot",
            thickness_px=thickness,
            is_thick=is_thick,
            confidence=0.6,
            p1=(float(pts[0][0]), float(pts[0][1])),
        )]
    ptsf = pts.astype(np.float32)

    line = _fit_straight(ptsf)
    if line is not None:
        p1, p2, dev, cap = line
        p1 = _snap_to_junction(p1, junction_points)
        p2 = _snap_to_junction(p2, junction_points)
        return [RawPrimitive(
            kind="segment",
            thickness_px=thickness,
            is_thick=is_thick,
            confidence=_fit_confidence(dev, cap),
            p1=(float(p1[0]), float(p1[1])),
            p2=(float(p2[0]), float(p2[1])),
        )]

    closed = bool(n > 8 and abs(int(pts[0][0]) - int(pts[-1][0])) <= 1
                  and abs(int(pts[0][1]) - int(pts[-1][1])) <= 1)
    if n >= 20:
        circ = _fit_circle_or_arc(ptsf, closed)
        if circ is not None:
            circ.thickness_px = thickness
            circ.is_thick = is_thick
            return [circ]

    approx = cv2.approxPolyDP(pts.reshape(-1, 1, 2), _POLYLINE_SIMPLIFY_EPS, closed)
    vertices = [(float(p[0][0]), float(p[0][1])) for p in approx]
    if len(vertices) < 2:
        vertices = [(float(ptsf[0][0]), float(ptsf[0][1])), (float(ptsf[-1][0]), float(ptsf[-1][1]))]

    # Corner chain vs genuine curve: straight edges between simplified
    # vertices are long; a smooth curve simplifies into many short edges.
    edges = list(zip(vertices[:-1], vertices[1:]))
    if closed and len(vertices) >= 3:
        edges.append((vertices[-1], vertices[0]))
    edge_lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in edges]
    if edges and min(edge_lens) >= _CORNER_SPLIT_MIN_EDGE_PX:
        return [
            RawPrimitive(
                kind="segment",
                thickness_px=thickness,
                is_thick=is_thick,
                confidence=0.7,
                p1=a,
                p2=b,
            )
            for a, b in edges
        ]

    return [RawPrimitive(
        kind="polyline",
        thickness_px=thickness,
        is_thick=is_thick,
        confidence=0.5,
        points=vertices,
        closed=closed,
    )]


def _draw_primitive(canvas, prim: RawPrimitive) -> None:
    import cv2
    import numpy as np

    t = prim.thickness_px
    if prim.kind == "dot":
        cv2.circle(
            canvas,
            (int(round(prim.p1[0])), int(round(prim.p1[1]))),
            max(1, t // 2), 0, -1, cv2.LINE_AA,
        )
    elif prim.kind == "segment":
        cv2.line(
            canvas,
            (int(round(prim.p1[0])), int(round(prim.p1[1]))),
            (int(round(prim.p2[0])), int(round(prim.p2[1]))),
            0, t, cv2.LINE_AA,
        )
    elif prim.kind == "circle":
        center = (int(round(prim.center[0])), int(round(prim.center[1])))
        cv2.circle(canvas, center, int(round(prim.radius)), 0, t, cv2.LINE_AA)
    elif prim.kind == "arc":
        center = (int(round(prim.center[0])), int(round(prim.center[1])))
        radius = int(round(prim.radius))
        cv2.ellipse(
            canvas, center, (radius, radius), 0.0,
            prim.start_angle, prim.end_angle, 0, t, cv2.LINE_AA,
        )
    elif prim.kind == "polyline":
        arr = np.array([[int(round(x)), int(round(y))] for x, y in prim.points], dtype=np.int32)
        cv2.polylines(canvas, [arr], prim.closed, 0, t, cv2.LINE_AA)


def _fit_straight(ptsf):
    """Least-squares line fit; returns (p1, p2, max_dev, cap) of the
    straightened stroke, or None when the path isn't straight. Direction snaps
    to the nearest canonical ЕСКД angle when already close; other angles stay
    as fitted (still perfectly straight, at their own angle)."""
    import numpy as np

    mean = ptsf.mean(axis=0)
    centered = ptsf - mean
    _u, _s, vt = np.linalg.svd(centered, full_matrices=False)
    direction, normal = vt[0], vt[1]
    max_dev = float(np.abs(centered @ normal).max())
    # Deviation allowance grows with length (diffusion wobble on a long line
    # reaches several px) up to the cap; an "arc" flat enough to pass the cap
    # has a multi-thousand-px radius — indistinguishable from straight at
    # sheet scale anyway.
    cap = min(_STRAIGHT_DEV_CAP_PX, max(2.5, 0.015 * len(ptsf)))
    if max_dev > cap:
        return None

    angle = math.degrees(math.atan2(float(direction[1]), float(direction[0]))) % 180.0
    canon = _nearest_canonical_angle(angle)
    if canon is not None:
        rad = math.radians(canon)
        direction = np.array([math.cos(rad), math.sin(rad)], dtype=np.float32)

    t = centered @ direction
    return mean + direction * float(t.min()), mean + direction * float(t.max()), max_dev, cap


def _nearest_canonical_angle(angle_deg: float) -> float | None:
    for canon in _CANONICAL_ANGLES_DEG:
        diff = min(
            abs(angle_deg - canon),
            abs(angle_deg - canon - 180.0),
            abs(angle_deg - canon + 180.0),
        )
        if diff <= _CANONICAL_TOLERANCE_DEG:
            return canon
    return None


def _snap_to_junction(p, junction_points):
    """Endpoints that met other strokes at a junction must keep meeting them
    after straightening — snap an endpoint back to its nearby junction."""
    import numpy as np

    if len(junction_points) == 0:
        return p
    d = np.linalg.norm(junction_points - p, axis=1)
    i = int(d.argmin())
    if float(d[i]) <= _JUNCTION_SNAP_RADIUS_PX:
        return junction_points[i]
    return p


def _taubin_circle(x, y):
    """Taubin's algebraic circle fit. Returns (cx, cy, r) or None.

    Kåsa's fit (plain least squares on x²+y²) minimizes an error weighted by
    distance from the centre, which pulls the circle inward whenever the
    points cover only a short arc: measured against ground truth, a drawn arc
    of 33° came back with its radius tens of pixels short and its centre
    displaced along the normal, even though the fitted curve tracked the ink
    and the start/end angles were right to a few degrees. Taubin removes that
    bias at essentially the same cost, and full circles — where Kåsa was
    already fine — are unaffected because the bias vanishes as coverage grows.
    """
    import numpy as np

    mean_x, mean_y = float(x.mean()), float(y.mean())
    u, v = x - mean_x, y - mean_y
    z = u * u + v * v
    m_zz = float((z * z).mean())
    m_xz, m_yz = float((u * z).mean()), float((v * z).mean())
    m_xx, m_yy, m_xy = float((u * u).mean()), float((v * v).mean()), float((u * v).mean())
    m_z = m_xx + m_yy
    cov_xy = m_xx * m_yy - m_xy * m_xy

    a3 = 4.0 * m_z
    a2 = -3.0 * m_z * m_z - m_zz
    a1 = m_zz * m_z + 4.0 * cov_xy * m_z - m_xz * m_xz - m_yz * m_yz - m_z**3
    a0 = (
        m_xz * m_xz * m_yy
        + m_yz * m_yz * m_xx
        - m_zz * cov_xy
        - 2.0 * m_xz * m_yz * m_xy
        + m_z * m_z * cov_xy
    )
    # Newton on the characteristic polynomial from the origin (Chernov): the
    # root sought is the smallest non-negative one, and starting at 0 with a
    # monotonicity guard converges to it in a handful of steps.
    root, previous = 0.0, float("inf")
    for _ in range(24):
        value = a0 + root * (a1 + root * (a2 + root * a3))
        if abs(value) > abs(previous):
            break
        previous = value
        derivative = a1 + root * (2.0 * a2 + root * 3.0 * a3)
        if derivative == 0.0:
            break
        step = value / derivative
        root = max(0.0, root - step)
        if abs(step) < 1e-12:
            break

    det = root * root - root * m_z + cov_xy
    if det == 0.0:
        return None
    cx = (m_xz * (m_yy - root) - m_yz * m_xy) / det / 2.0
    cy = (m_yz * (m_xx - root) - m_xz * m_xy) / det / 2.0
    r_sq = cx * cx + cy * cy + m_z
    if not np.isfinite(r_sq) or r_sq <= 0.0:
        return None
    return cx + mean_x, cy + mean_y, math.sqrt(r_sq)


def _fit_circle_or_arc(ptsf, closed: bool) -> RawPrimitive | None:
    """Taubin algebraic circle fit; returns a circle (closed path) or arc
    (open path) primitive when the points genuinely lie on one, else None —
    caller falls back to a polyline. Thickness fields are filled in by the
    caller."""
    import numpy as np

    x, y = ptsf[:, 0], ptsf[:, 1]
    span = float(max(x.max() - x.min(), y.max() - y.min()))
    fit = _taubin_circle(x, y)
    if fit is None:
        return None
    cx, cy, r = fit
    if r <= _MIN_CIRCLE_RADIUS_PX:
        return None
    # A radius far beyond the path's own extent means "almost straight" — the
    # fit is numerically valid but meaningless as a drawable circle.
    if r > 4 * max(span, 1.0):
        return None
    dev = np.abs(np.hypot(x - cx, y - cy) - r)
    dev_cap = max(_CIRCLE_DEV_CAP_PX, 0.01 * r)
    if float(dev.max()) > dev_cap:
        return None

    confidence = _fit_confidence(float(dev.max()), dev_cap)
    if closed:
        return RawPrimitive(
            kind="circle",
            thickness_px=1,
            is_thick=False,
            confidence=confidence,
            center=(float(cx), float(cy)),
            radius=float(r),
        )
    angles = np.degrees(np.unwrap(np.arctan2(y - cy, x - cx)))
    a0, a1 = float(angles[0]), float(angles[-1])
    if abs(a1 - a0) > 370.0:
        return None
    # Canonicalize to the DXF/CadIR convention: an arc sweeps CCW from start to
    # end. The traced chain may run either way around the curve, which would
    # otherwise emit start/end reversed — geometrically the same arc (the PNG
    # render is order-agnostic) but a mismatch for anything that reads the
    # convention (native-DXF entity comparison, downstream angle math). The
    # unwrapped samples are monotonic, so ordering the endpoints puts the same
    # covered points in CCW order.
    if a1 < a0:
        a0, a1 = a1, a0
    return RawPrimitive(
        kind="arc",
        thickness_px=1,
        is_thick=False,
        confidence=confidence,
        center=(float(cx), float(cy)),
        radius=float(r),
        start_angle=a0,
        end_angle=a1,
    )


# ── Self-verification ────────────────────────────────────────────────────────


def _verify_coverage(ink_bool, redrawn) -> bool:
    """Both directions within a small dilation: recall (original ink near
    redrawn strokes — nothing real got lost) and precision (redrawn strokes
    near original ink — nothing got hallucinated/displaced). This is the
    hard guarantee that a corrupted redraw cannot ship: it declines instead."""
    import cv2
    import numpy as np

    k = 2 * _coverage_dilate_px(*ink_bool.shape[:2]) + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    redrawn_grown = cv2.dilate(redrawn.astype(np.uint8), kernel) > 0
    ink_grown = cv2.dilate(ink_bool.astype(np.uint8), kernel) > 0

    ink_total = int(ink_bool.sum())
    redrawn_total = int(redrawn.sum())
    if ink_total == 0 or redrawn_total == 0:
        logger.info("vectorize_declined_empty_verify")
        return False
    recall = float((redrawn_grown & ink_bool).sum()) / ink_total
    precision = float((ink_grown & redrawn).sum()) / redrawn_total
    if recall < _MIN_COVERAGE_RECALL or precision < _MIN_COVERAGE_PRECISION:
        logger.warning(
            "vectorize_declined_coverage",
            recall=round(recall, 3),
            precision=round(precision, 3),
        )
        return False
    logger.info(
        "vectorize_verified", recall=round(recall, 3), precision=round(precision, 3)
    )
    return True
