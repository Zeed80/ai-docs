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
    label = "многовидовая реконструкция" if len(available_views) >= 2 else "одновидовая гипотеза"
    return [MultiViewCandidate(
        label=label, score=round(score, 3), supporting_views=sorted(available_views),
        features=reconstructed, missing_data=sorted(set(unresolved)),
    )]
