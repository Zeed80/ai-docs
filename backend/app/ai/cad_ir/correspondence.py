"""Correspondence graph between orthographic drawing views (D1).

A single view is ambiguous; ГОСТ 2.305 orthographic projection resolves that
ambiguity by placing related views in fixed alignment and repeating the same
feature across them. This module builds the deterministic correspondence
graph between views — which features in different views are the SAME physical
feature — from four kinds of evidence:

- **axis alignment**: front↔top views share the horizontal axis, front↔side
  share the vertical one; a feature's projected position must line up across
  the aligned axis.
- **diameter ↔ circle**: a Ø dimension in one view and a circle of the
  matching size in an orthogonal view are one cylindrical feature.
- **hidden ↔ visible**: a hidden (dashed) contour in one view is a feature
  that reads as a visible circle/edge in the view looking along its axis.
- **scale consistency**: every view should share one drawing scale; a
  divergent view scale is surfaced, not silently trusted.

Pure, deterministic, decoupled from the DB models so it is unit-testable and
reusable by both the drawings pipeline and the CAD-IR multiview path.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ViewCircle:
    cx: float
    cy: float
    r: float  # pixels


@dataclass
class ViewGeometry:
    """One orthographic view's recognized content, in its own pixel space."""

    label: str
    projection: str  # "front" | "top" | "side" | "section" | "isometric" | ...
    scale: float | None = None  # mm per px for this view
    circles: list[ViewCircle] = field(default_factory=list)
    # labelled diameters read on this view, in mm
    diameters_mm: list[float] = field(default_factory=list)
    # centerline axis x-positions (vertical axes) and y-positions (horizontal)
    axes_x: list[float] = field(default_factory=list)
    axes_y: list[float] = field(default_factory=list)
    # bounding box of the view's geometry in px (x0, y0, x1, y1)
    bbox: tuple[float, float, float, float] | None = None
    # does the view contain hidden (dashed) contour lines?
    has_hidden: bool = False


@dataclass
class Correspondence:
    kind: str  # "axis_alignment" | "diameter" | "hidden_visible" | "scale"
    views: tuple[str, str]
    detail: str
    confidence: float


@dataclass
class CorrespondenceGraph:
    correspondences: list[Correspondence] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def confirmed_view_pairs(self) -> set[tuple[str, str]]:
        return {
            tuple(sorted(c.views))
            for c in self.correspondences
            if c.kind in ("axis_alignment", "diameter", "hidden_visible")
        }


# Orthographic view pairs that share an axis (ГОСТ 2.305, first-angle):
# front–top align along X (vertical alignment), front–side along Y.
_X_ALIGNED = {("front", "top"), ("top", "front")}
_Y_ALIGNED = {("front", "side"), ("side", "front")}

_SCALE_REL_TOL = 0.05        # 5% between view scales
_DIAMETER_REL_TOL = 0.08     # circle Ø vs labelled Ø
_AXIS_ALIGN_REL_TOL = 0.06   # shared-axis centre alignment, fraction of span


def _bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _bbox_span(bbox: tuple[float, float, float, float]) -> float:
    return max(bbox[2] - bbox[0], bbox[3] - bbox[1], 1.0)


def build_correspondence_graph(views: list[ViewGeometry]) -> CorrespondenceGraph:
    graph = CorrespondenceGraph()
    if len(views) < 2:
        return graph

    # 1. Scale consistency — one drawing scale across all views.
    scaled = [v for v in views if v.scale]
    for i in range(len(scaled)):
        for j in range(i + 1, len(scaled)):
            a, b = scaled[i], scaled[j]
            rel = abs(a.scale - b.scale) / max(a.scale, b.scale)
            if rel <= _SCALE_REL_TOL:
                graph.correspondences.append(Correspondence(
                    "scale", (a.label, b.label),
                    f"масштабы согласованы ({a.scale:.4g}≈{b.scale:.4g} мм/px)",
                    round(1.0 - rel, 3),
                ))
            else:
                graph.issues.append(
                    f"Масштабы видов «{a.label}» и «{b.label}» расходятся "
                    f"({a.scale:.4g} vs {b.scale:.4g} мм/px)"
                )

    for i in range(len(views)):
        for j in range(i + 1, len(views)):
            a, b = views[i], views[j]
            pair = (a.projection, b.projection)

            # 2. Axis alignment across an orthographic pair.
            if a.bbox and b.bbox:
                ca, cb = _bbox_center(a.bbox), _bbox_center(b.bbox)
                span = max(_bbox_span(a.bbox), _bbox_span(b.bbox))
                if pair in _X_ALIGNED and abs(ca[0] - cb[0]) <= _AXIS_ALIGN_REL_TOL * span:
                    graph.correspondences.append(Correspondence(
                        "axis_alignment", (a.label, b.label),
                        "виды выровнены по вертикальной оси проекции", 0.9,
                    ))
                elif pair in _Y_ALIGNED and abs(ca[1] - cb[1]) <= _AXIS_ALIGN_REL_TOL * span:
                    graph.correspondences.append(Correspondence(
                        "axis_alignment", (a.label, b.label),
                        "виды выровнены по горизонтальной оси проекции", 0.9,
                    ))
                elif pair in _X_ALIGNED or pair in _Y_ALIGNED:
                    graph.issues.append(
                        f"Виды «{a.label}» и «{b.label}» должны быть выровнены "
                        f"по оси проекции, но смещены"
                    )

            # 3. Diameter ↔ circle: a Ø label in one view matches a circle in
            #    the other (the cylindrical feature seen end-on).
            for src, dst in ((a, b), (b, a)):
                for d in src.diameters_mm:
                    for c in dst.circles:
                        if not dst.scale:
                            continue
                        circle_d = 2 * c.r * dst.scale
                        if abs(circle_d - d) <= _DIAMETER_REL_TOL * max(d, 1e-6):
                            graph.correspondences.append(Correspondence(
                                "diameter", (src.label, dst.label),
                                f"Ø{d:g} мм подтверждён окружностью в «{dst.label}»",
                                0.85,
                            ))
                            break

            # 4. Hidden ↔ visible: a hidden (dashed) contour in one view is a
            #    feature read as a visible circle in the orthogonal view.
            for src, dst in ((a, b), (b, a)):
                if src.has_hidden and dst.circles:
                    graph.correspondences.append(Correspondence(
                        "hidden_visible", (src.label, dst.label),
                        f"скрытый контур в «{src.label}» соответствует "
                        f"отверстию, видимому в «{dst.label}»",
                        0.7,
                    ))

    # de-duplicate identical hidden_visible edges (both directions add one)
    seen: set[tuple[str, tuple[str, str], str]] = set()
    unique: list[Correspondence] = []
    for c in graph.correspondences:
        key = (c.kind, tuple(sorted(c.views)), c.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    graph.correspondences = unique
    return graph


def correspondence_notes(graph: CorrespondenceGraph) -> list[str]:
    """Human-readable summary lines for the review UI."""
    return [c.detail for c in graph.correspondences] + graph.issues
