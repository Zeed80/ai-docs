"""Deterministic engineering interpretation graph over CadIR.

The graph separates observations from engineering hypotheses.  It never
creates export geometry: recognizers observe entities, this module relates
them, and a later CAD solver may turn only constraint-validated hypotheses
into parametric features.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field

from app.ai.cad_ir.schema import (
    Arc,
    CadIR,
    Circle,
    DimensionEntity,
    Entity,
    HatchRegion,
    Polyline,
    Segment,
)


class GraphRegion(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class ViewNode(BaseModel):
    id: str
    kind: Literal["front", "top", "side", "section", "detail", "unknown"]
    region: GraphRegion
    entity_ids: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class FeatureHypothesis(BaseModel):
    id: str
    kind: Literal[
        "hole",
        "hole_pattern",
        "counterbore",
        "outer_profile",
        "section",
        "wall",
        "opening",
    ]
    entity_ids: list[str]
    view_ids: list[str]
    parameters: dict[str, float | int | str] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["observed", "inferred", "constraint_validated"] = "inferred"
    evidence: list[str] = Field(default_factory=list)


class DimensionRelation(BaseModel):
    dimension_id: str
    relation: Literal["distance", "diameter", "radius", "angle", "unresolved"]
    target_entity_ids: list[str] = Field(default_factory=list)
    value_mm: float | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class EngineeringGraph(BaseModel):
    profile: Literal["mechanical", "construction", "auto"]
    views: list[ViewNode] = Field(default_factory=list)
    features: list[FeatureHypothesis] = Field(default_factory=list)
    dimensions: list[DimensionRelation] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    exact_ready: bool = False


def _bbox(entity: Entity) -> tuple[float, float, float, float] | None:
    if isinstance(entity, Segment):
        points = [entity.p1, entity.p2]
    elif isinstance(entity, (Circle, Arc)):
        return (
            entity.center.x - entity.radius,
            entity.center.y - entity.radius,
            entity.center.x + entity.radius,
            entity.center.y + entity.radius,
        )
    elif isinstance(entity, Polyline):
        points = entity.points
    elif isinstance(entity, HatchRegion):
        points = entity.boundary
    else:
        return None
    return (
        min(point.x for point in points),
        min(point.y for point in points),
        max(point.x for point in points),
        max(point.y for point in points),
    )


def _regions_touch(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    gap: float,
) -> bool:
    return not (
        left[2] + gap < right[0]
        or right[2] + gap < left[0]
        or left[3] + gap < right[1]
        or right[3] + gap < left[1]
    )


def _view_components(ir: CadIR) -> list[list[Entity]]:
    width, height = ir.source.image_width, ir.source.image_height
    candidates = []
    for entity in ir.entities:
        bounds = _bbox(entity)
        if bounds is None:
            continue
        # Sheet frames and title-block rules are document furniture, not views.
        if isinstance(entity, Segment):
            length = math.hypot(
                entity.p2.x - entity.p1.x,
                entity.p2.y - entity.p1.y,
            )
            if length > 0.78 * max(width, height):
                continue
        candidates.append((entity, bounds))
    parent = list(range(len(candidates)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    gap = 0.012 * max(width, height)
    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            if _regions_touch(candidates[left][1], candidates[right][1], gap):
                union(left, right)
    groups: dict[int, list[Entity]] = defaultdict(list)
    for index, (entity, _bounds) in enumerate(candidates):
        groups[find(index)].append(entity)
    return [group for group in groups.values() if group]


def _view_nodes(ir: CadIR) -> list[ViewNode]:
    components = _view_components(ir)
    raw = []
    for index, entities in enumerate(components):
        boxes = [_bbox(entity) for entity in entities]
        boxes = [box for box in boxes if box is not None]
        region = GraphRegion(
            x0=min(box[0] for box in boxes),
            y0=min(box[1] for box in boxes),
            x1=max(box[2] for box in boxes),
            y1=max(box[3] for box in boxes),
        )
        area = max(region.x1 - region.x0, 1.0) * max(region.y1 - region.y0, 1.0)
        has_hatch = any(isinstance(entity, HatchRegion) for entity in entities)
        raw.append((index, entities, region, area, has_hatch))
    if not raw:
        return []
    largest = max(raw, key=lambda item: item[3])
    largest_center = (
        (largest[2].x0 + largest[2].x1) / 2,
        (largest[2].y0 + largest[2].y1) / 2,
    )
    views = []
    for index, entities, region, _area, has_hatch in raw:
        center = ((region.x0 + region.x1) / 2, (region.y0 + region.y1) / 2)
        if has_hatch:
            kind, confidence, evidence = "section", 0.9, ["hatch-region"]
        elif index == largest[0]:
            kind, confidence, evidence = "front", 0.7, ["largest-view-component"]
        elif abs(center[0] - largest_center[0]) < 0.15 * ir.source.image_width:
            kind, confidence, evidence = "top", 0.6, ["projection-x-alignment"]
        elif abs(center[1] - largest_center[1]) < 0.15 * ir.source.image_height:
            kind, confidence, evidence = "side", 0.6, ["projection-y-alignment"]
        else:
            kind, confidence, evidence = "unknown", 0.3, ["spatial-component"]
        views.append(
            ViewNode(
                id=f"view-{index}",
                kind=kind,
                region=region,
                entity_ids=[entity.id for entity in entities],
                confidence=confidence,
                evidence=evidence,
            )
        )
    return views


def _view_for_entity(views: list[ViewNode], entity_id: str) -> str | None:
    return next((view.id for view in views if entity_id in view.entity_ids), None)


def _dimension_relations(ir: CadIR) -> list[DimensionRelation]:
    geometry = [entity for entity in ir.entities if _bbox(entity) is not None]
    relations = []
    for dimension in (
        entity for entity in ir.entities if isinstance(entity, DimensionEntity)
    ):
        center = (
            (dimension.p1.x + dimension.p2.x) / 2,
            (dimension.p1.y + dimension.p2.y) / 2,
        )
        ranked = []
        for entity in geometry:
            bounds = _bbox(entity)
            if bounds is None:
                continue
            entity_center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
            ranked.append((math.dist(center, entity_center), entity))
        nearest = min(ranked, default=(0.0, None), key=lambda item: item[0])[1]
        relation = {
            "diameter": "diameter",
            "radial": "radius",
            "angular": "angle",
            "linear": "distance",
        }[dimension.kind]
        relations.append(
            DimensionRelation(
                dimension_id=dimension.id,
                relation=relation if nearest else "unresolved",
                target_entity_ids=[nearest.id] if nearest else [],
                value_mm=dimension.value_mm,
                confidence=0.65 if nearest else 0.0,
                evidence=["nearest-geometry-anchor"] if nearest else [],
            )
        )
    return relations


def _mechanical_features(ir: CadIR, views: list[ViewNode]) -> list[FeatureHypothesis]:
    circles = [entity for entity in ir.entities if isinstance(entity, Circle)]
    features = [
        FeatureHypothesis(
            id=f"hole-{circle.id}",
            kind="hole",
            entity_ids=[circle.id],
            view_ids=[view_id] if (view_id := _view_for_entity(views, circle.id)) else [],
            parameters={"radius_px": circle.radius},
            confidence=0.75,
            status="observed",
            evidence=["circle-observation"],
        )
        for circle in circles
    ]
    radius_buckets: dict[int, list[Circle]] = defaultdict(list)
    for circle in circles:
        radius_buckets[round(circle.radius)].append(circle)
    for radius, members in radius_buckets.items():
        if len(members) < 2:
            continue
        members_verified = all(
            circle.assurance in ("constraint_validated", "human_approved")
            for circle in members
        )
        view_ids = sorted(
            {
                view_id
                for circle in members
                if (view_id := _view_for_entity(views, circle.id))
            }
        )
        features.append(
            FeatureHypothesis(
                id=f"hole-pattern-r{radius}",
                kind="hole_pattern",
                entity_ids=[circle.id for circle in members],
                view_ids=view_ids,
                parameters={"count": len(members), "radius_px": radius},
                confidence=0.8,
                status="constraint_validated" if members_verified else "inferred",
                evidence=[
                    "equal-radii",
                    "repeated-circle-observations",
                    *(
                        ["verified-member-geometry"]
                        if members_verified
                        else []
                    ),
                ],
            )
        )
    return features


def build_engineering_graph(
    ir: CadIR,
    *,
    profile: Literal["mechanical", "construction", "auto"] = "auto",
) -> EngineeringGraph:
    views = _view_nodes(ir)
    dimensions = _dimension_relations(ir)
    features = _mechanical_features(ir, views) if profile != "construction" else []
    unresolved = []
    if not views:
        unresolved.append("no-view-components")
    unresolved.extend(
        f"dimension:{relation.dimension_id}"
        for relation in dimensions
        if relation.relation == "unresolved"
    )
    exact_ready = bool(views) and not unresolved and all(
        feature.status != "inferred" for feature in features
    )
    return EngineeringGraph(
        profile=profile,
        views=views,
        features=features,
        dimensions=dimensions,
        unresolved=unresolved,
        exact_ready=exact_ready,
    )
