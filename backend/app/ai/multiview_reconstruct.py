"""Ranked, evidence-preserving 3D hypotheses from analysed drawing views."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.db.models import DrawingFeature, DrawingViewSection, FeatureDimType


class MultiViewFeature(BaseModel):
    feature_id: str
    kind: str
    supporting_views: list[str] = Field(default_factory=list)
    depth_mm: float | None = None
    confidence: float


class MultiViewCandidate(BaseModel):
    label: str
    score: float
    supporting_views: list[str]
    features: list[MultiViewFeature]
    missing_data: list[str] = Field(default_factory=list)
    # D1: correspondence-graph evidence between views (axes/diameters/hidden/
    # scale), as human-readable notes.
    correspondences: list[str] = Field(default_factory=list)


def _bbox_tuple(bbox) -> tuple[float, float, float, float] | None:
    """Normalize a stored bbox dict/list to (x0, y0, x1, y1)."""
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    if isinstance(bbox, dict):
        if all(k in bbox for k in ("x0", "y0", "x1", "y1")):
            return (float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"]))
        if all(k in bbox for k in ("x", "y", "w", "h")):
            x, y, w, h = (float(bbox[k]) for k in ("x", "y", "w", "h"))
            return (x, y, x + w, y + h)
    return None


def _view_geometries(views: list[DrawingViewSection]):
    """Adapt DB view rows into correspondence.ViewGeometry. Axis alignment is
    derivable from the fields every view row already stores (bbox_on_sheet +
    section_type); the richer circle/diameter/hidden inputs come from the
    optional ``geometry`` meta the segmentation attaches when present."""
    from app.ai.cad_ir.correspondence import ViewCircle, ViewGeometry

    out = []
    for view in views:
        meta = getattr(view, "geometry", None)
        meta = meta if isinstance(meta, dict) else {}
        out.append(ViewGeometry(
            label=view.section_label or view.section_type,
            projection=(meta.get("projection") or view.section_type or "").lower(),
            scale=meta.get("scale_mm_per_px"),
            circles=[ViewCircle(**c) for c in meta.get("circles", []) if isinstance(c, dict)],
            diameters_mm=list(meta.get("diameters_mm", [])),
            axes_x=list(meta.get("axes_x", [])),
            axes_y=list(meta.get("axes_y", [])),
            bbox=_bbox_tuple(view.bbox_on_sheet),
            has_hidden=bool(meta.get("has_hidden")),
        ))
    return out


def reconstruct_from_views(
    features: list[DrawingFeature], views: list[DrawingViewSection]
) -> list[MultiViewCandidate]:
    """Build candidates without pretending that an unobserved depth is fact.

    A feature seen in two orthogonal/section views is confirmed. A feature
    with a depth dimension is deterministically parameterized. Everything
    else remains an explicit hypothesis with the missing evidence surfaced.
    """
    available_views = {view.section_label or view.section_type for view in views}
    reconstructed: list[MultiViewFeature] = []
    unresolved: list[str] = []
    scores: list[float] = []
    for feature in features:
        supporting = sorted({item for item in (feature.confirmed_by_views or []) if item} | ({feature.source_view} if feature.source_view else set()))
        depth = next((dimension.nominal for dimension in feature.dimensions if dimension.dim_type == FeatureDimType.depth), None)
        confirmed = len(supporting) >= 2 or any("section" in item.lower() or "-" in item for item in supporting)
        confidence = min(1.0, feature.confidence + (0.15 if confirmed else 0.0) + (0.1 if depth is not None else 0.0))
        reconstructed.append(MultiViewFeature(
            feature_id=str(feature.id), kind=feature.feature_type.value,
            supporting_views=supporting, depth_mm=depth, confidence=round(confidence, 3),
        ))
        scores.append(confidence)
        if depth is None and feature.feature_type.value in {"hole", "pocket", "boss", "groove", "slot"}:
            unresolved.append(f"глубина элемента «{feature.name}» не задана")
        if not confirmed and len(available_views) < 2:
            unresolved.append(f"для «{feature.name}» нужен второй вид или разрез")
    if not reconstructed:
        return []
    score = sum(scores) / len(scores)
    if len(available_views) >= 2:
        score = min(1.0, score + 0.05)

    # D1: build the correspondence graph between views; confirmed
    # correspondences raise confidence, scale/axis mismatches surface as
    # missing_data — never silently trusted.
    correspondence_notes: list[str] = []
    try:
        from app.ai.cad_ir.correspondence import build_correspondence_graph, correspondence_notes as _notes

        graph = build_correspondence_graph(_view_geometries(views))
        correspondence_notes = _notes(graph)
        if graph.confirmed_view_pairs:
            score = min(1.0, score + 0.05 * len(graph.confirmed_view_pairs))
        for issue in graph.issues:
            unresolved.append(issue)
    except Exception:  # noqa: BLE001 — enrichment must never break reconstruction
        correspondence_notes = []

    label = "многовидовая реконструкция" if len(available_views) >= 2 else "одновидовая гипотеза"
    return [MultiViewCandidate(
        label=label, score=round(score, 3), supporting_views=sorted(available_views),
        features=reconstructed, missing_data=sorted(set(unresolved)),
        correspondences=correspondence_notes,
    )]
