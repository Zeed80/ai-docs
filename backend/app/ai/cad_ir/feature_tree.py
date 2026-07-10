"""2D CAD IR -> ranked 3D feature-tree HYPOTHESES (Ф10).

Per the critique: reconstructing a 3D model from a single 2D orthographic
view is fundamentally under-determined — depth along the view axis is
invisible in the drawing. This module does NOT compute "the" 3D model. It
proposes several candidate feature trees, each with a confidence-like score
and an explicit list of missing data that would resolve the ambiguity (a
side view, a stated depth dimension, a section marker). The human picks;
nothing here is asserted as fact — every extrude-depth candidate below a
stated-dimension match is a labeled GUESS, not a measurement.

Canon is the returned ``FeatureTreeCandidate`` (a feature/constraint graph
over the SAME 2D IR entities, via ``source_entity_ids``), not any compiled
solid — ``compile_to_step`` is a COMPILER TARGET for a human-CONFIRMED
candidate, never the source of truth. It degrades honestly (returns None,
never a fake result) when the cad-kernel (CadQuery/OCP) isn't available in
this environment.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.ai.cad_ir.schema import CadIR, Circle, DimensionEntity, TextEntity

_DEPTH_PATTERN = re.compile(r"(?:глубина|толщина|depth|h)\s*[=:]?\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_THROUGH_PATTERN = re.compile(r"сквозн|through", re.IGNORECASE)


class Feature3D(BaseModel):
    kind: Literal["extrude", "hole", "boss"]
    source_entity_ids: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.5


class FeatureTreeCandidate(BaseModel):
    features: list[Feature3D]
    score: float
    label: str
    missing_data: list[str] = Field(default_factory=list)


def _footprint_mm(ir: CadIR) -> tuple[float, float] | None:
    """Bounding box of the main-weight contour geometry, in mm — the part's
    footprint as seen from this view. A simplification: the true outer
    silhouette would need real contour tracing (shapely union of the
    contour entities); the bounding box is a coarser but honest stand-in,
    good enough for depth-guess heuristics that are approximate by
    construction anyway."""
    scale = ir.scale or 1.0
    xs: list[float] = []
    ys: list[float] = []
    for e in ir.entities:
        if e.line_class != "contour" or e.width_class != "main":
            continue
        if e.type == "segment":
            xs += [e.p1.x, e.p2.x]
            ys += [e.p1.y, e.p2.y]
        elif e.type == "circle":
            xs += [e.center.x - e.radius, e.center.x + e.radius]
            ys += [e.center.y - e.radius, e.center.y + e.radius]
        elif e.type in ("arc", "polyline"):
            pts = e.points if e.type == "polyline" else []
            if e.type == "arc":
                xs += [e.center.x - e.radius, e.center.x + e.radius]
                ys += [e.center.y - e.radius, e.center.y + e.radius]
            else:
                xs += [p.x for p in pts]
                ys += [p.y for p in pts]
    if not xs or not ys:
        return None
    return (max(xs) - min(xs)) * scale, (max(ys) - min(ys)) * scale


def _stated_depth_mm(ir: CadIR) -> float | None:
    """A depth actually written on the sheet (dimension/text) beats any
    geometric guess — real data, not a heuristic."""
    for e in ir.entities:
        text = getattr(e, "text", None)
        if not text:
            continue
        m = _DEPTH_PATTERN.search(text)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                continue
    return None


def _hole_features(ir: CadIR) -> list[Feature3D]:
    scale = ir.scale or 1.0
    features: list[Feature3D] = []
    for e in ir.entities:
        if not isinstance(e, Circle):
            continue
        diameter_mm = 2 * e.radius * scale
        through = any(
            isinstance(other, (TextEntity, DimensionEntity))
            and getattr(other, "text", None)
            and _THROUGH_PATTERN.search(other.text)
            for other in ir.entities
        )
        features.append(Feature3D(
            kind="hole",
            source_entity_ids=[e.id],
            params={"diameter_mm": diameter_mm, "through": through if through else None},
            confidence=0.8 if through else 0.5,
        ))
    return features


# Depth-guess heuristics, each a distinct, labeled hypothesis — deliberately
# NOT trying to pick "the best one": that decision needs data this module
# doesn't have (a side view), so it's left to the human.
_DEPTH_HEURISTICS: tuple[tuple[str, float, str], ...] = (
    ("square", 1.0, "глубина = меньшая сторона footprint (предположение о квадратном сечении)"),
    ("half_width", 0.5, "глубина = половина ширины footprint (предположение)"),
)


def generate_feature_tree_candidates(ir: CadIR) -> list[FeatureTreeCandidate]:
    footprint = _footprint_mm(ir)
    if footprint is None:
        return []
    width_mm, height_mm = footprint
    holes = _hole_features(ir)
    hole_missing = [] if not holes else [
        f"глубина отверстия {h.params['diameter_mm']:g}мм не указана на чертеже (сквозное/глухое)"
        for h in holes if h.params.get("through") is None
    ]

    stated_depth = _stated_depth_mm(ir)
    candidates: list[FeatureTreeCandidate] = []

    if stated_depth is not None:
        base = Feature3D(
            kind="extrude", source_entity_ids=[], confidence=0.9,
            params={"depth_mm": stated_depth, "width_mm": width_mm, "height_mm": height_mm},
        )
        candidates.append(FeatureTreeCandidate(
            features=[base, *holes], score=0.9,
            label=f"глубина {stated_depth:g}мм — указана на чертеже",
            missing_data=hole_missing,
        ))

    for name, ratio, label in _DEPTH_HEURISTICS:
        depth = min(width_mm, height_mm) * ratio if name == "square" else width_mm * ratio
        base = Feature3D(
            kind="extrude", source_entity_ids=[], confidence=0.2,
            params={"depth_mm": depth, "width_mm": width_mm, "height_mm": height_mm},
        )
        candidates.append(FeatureTreeCandidate(
            features=[base, *holes], score=0.2, label=label,
            missing_data=["нет бокового вида/разреза — глубина выдавливания не измерена, это эвристика", *hole_missing],
        ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def compile_to_step(candidate: FeatureTreeCandidate) -> bytes | None:
    """Compile a HUMAN-CONFIRMED candidate to a STEP file via CadQuery.

    Returns None (never raises, never fakes a result) when the cad-kernel
    (CadQuery/OCP — a heavy native dependency, deliberately NOT installed in
    the main backend image) isn't available in this environment. A real
    deployment runs this in a dedicated `cad-kernel` container, same
    isolation pattern as `skill-runner` — building/wiring that container is
    explicitly NOT done in this pass (unverified without a live environment
    to build and smoke-test it in); this function is the integration point
    ready for it.
    """
    try:
        import cadquery as cq
    except ImportError:
        return None

    import tempfile
    from pathlib import Path

    result = None
    for feature in candidate.features:
        if feature.kind == "extrude":
            w = feature.params.get("width_mm", 10.0)
            h = feature.params.get("height_mm", 10.0)
            d = feature.params.get("depth_mm", 10.0)
            result = cq.Workplane("XY").box(w, h, d)
    if result is None:
        return None
    for feature in candidate.features:
        if feature.kind == "hole":
            dia = feature.params.get("diameter_mm", 5.0)
            result = result.faces(">Z").workplane().hole(dia)

    # cadquery.exporters.export writes to a path, not a byte buffer — round
    # trip through a temp file (untested in this session: cadquery isn't
    # installed here, see the module docstring).
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "candidate.step"
        cq.exporters.export(result, str(out_path))
        return out_path.read_bytes()
