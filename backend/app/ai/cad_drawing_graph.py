"""Strict source drawing graph and interpretation-free CadIR drafter.

The graph is the contract between coordinate recognition and redrawing. It
contains observations, stable identifiers and semantic relations for the full
sheet. The drafter below performs a one-to-one copy; it never recognizes,
repairs or invents geometry.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.ai.cad_ir.schema import (
    AnnotationEntity,
    Arc,
    CadEntityRelation,
    CadIR,
    Circle,
    DimensionEntity,
    Entity,
    HatchRegion,
    Point,
    Polyline,
    Segment,
    SheetInfo,
    SourceInfo,
    SourceRegion,
    TextEntity,
    UnresolvedRegion,
)


class DrawingGraphEvidence(BaseModel):
    """Auditable source observation in full-sheet pixel coordinates."""

    id: str = Field(min_length=1, max_length=120)
    kind: Literal[
        "pixel_support",
        "ocr",
        "geometry_detector",
        "symbol_detector",
        "relation_model",
        "constraint_check",
        "human",
    ]
    region: SourceRegion
    image_index: int = Field(default=0, ge=0)
    raw_text: str | None = None
    model_key: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class DrawingGraphSource(BaseModel):
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    kind: Literal["scan", "photo", "pdf_page", "import"] = "scan"
    page_index: int = Field(default=0, ge=0)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class DrawingGraphView(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    kind: Literal[
        "sheet",
        "front",
        "top",
        "side",
        "section",
        "detail",
        "title_block",
        "table",
        "unknown",
    ]
    region: SourceRegion
    entity_ids: list[str] = Field(default_factory=list)
    parent_view_id: str | None = None
    label: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class DrawingGraphRelation(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    kind: Literal[
        "connected",
        "coincident",
        "parallel",
        "perpendicular",
        "tangent",
        "concentric",
        "equal",
        "dimension_applies_to",
        "annotation_applies_to",
        "same_feature_across_views",
        "projection_alignment",
        "part_of",
    ]
    source_entity_id: str
    target_entity_ids: list[str] = Field(min_length=1)
    parameters: dict[str, float | int | str | bool] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    assurance: Literal[
        "observed", "inferred", "constraint_validated", "human_approved"
    ] = "inferred"
    evidence: list[str] = Field(default_factory=list)


class EngineeringDrawingGraph(BaseModel):
    """Complete coordinate and semantic description of one source sheet."""

    schema_version: Literal[1] = 1
    graph_status: Literal["reader_output", "verified", "human_reviewed"] = (
        "reader_output"
    )
    source: DrawingGraphSource
    scale_mm_per_px: float | None = Field(default=None, gt=0)
    scale_source: Literal["manual", "calibration", "dpi", "sheet_format"] | None = None
    sheet: SheetInfo = Field(default_factory=SheetInfo)
    evidence: list[DrawingGraphEvidence] = Field(default_factory=list)
    views: list[DrawingGraphView] = Field(min_length=1)
    entities: list[Entity] = Field(min_length=1)
    relations: list[DrawingGraphRelation] = Field(default_factory=list)
    unresolved_regions: list[UnresolvedRegion] = Field(default_factory=list)
    reader_manifest: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_graph_integrity(self) -> "EngineeringDrawingGraph":
        entity_ids = [entity.id for entity in self.entities]
        view_ids = [view.id for view in self.views]
        evidence_ids = [item.id for item in self.evidence]
        relation_ids = [relation.id for relation in self.relations]
        for label, values in (
            ("entity", entity_ids),
            ("view", view_ids),
            ("evidence", evidence_ids),
            ("relation", relation_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"drawing graph {label} ids must be unique")

        known_entities = set(entity_ids)
        known_views = set(view_ids)
        known_evidence = set(evidence_ids)
        owned: list[str] = []
        for view in self.views:
            missing = sorted(set(view.entity_ids) - known_entities)
            if missing:
                raise ValueError(f"view {view.id} references missing entities: {missing}")
            if view.parent_view_id and view.parent_view_id not in known_views:
                raise ValueError(f"view {view.id} references missing parent view")
            self._validate_evidence_refs(view.id, view.evidence, known_evidence)
            self._validate_region(view.id, view.region)
            owned.extend(view.entity_ids)
        if sorted(owned) != sorted(entity_ids):
            raise ValueError("every graph entity must belong to exactly one view")

        for item in self.evidence:
            self._validate_region(item.id, item.region)
        for entity in self.entities:
            self._validate_entity_bounds(entity)
            self._validate_evidence_refs(entity.id, entity.evidence, known_evidence)
            if entity.origin != "human" and not entity.evidence:
                raise ValueError(f"entity {entity.id} has no source evidence")
            if self.graph_status == "reader_output" and entity.assurance not in (
                "observed", "inferred"
            ):
                raise ValueError("reader output cannot self-assign validated assurance")
        for relation in self.relations:
            refs = {relation.source_entity_id, *relation.target_entity_ids}
            missing = sorted(refs - known_entities)
            if missing:
                raise ValueError(
                    f"relation {relation.id} references missing entities: {missing}"
                )
            self._validate_evidence_refs(relation.id, relation.evidence, known_evidence)
            if self.graph_status == "reader_output" and relation.assurance not in (
                "observed", "inferred"
            ):
                raise ValueError("reader relation cannot self-assign validated assurance")

        dimension_ids = {
            entity.id for entity in self.entities if isinstance(entity, DimensionEntity)
        }
        related_dimensions = {
            relation.source_entity_id
            for relation in self.relations
            if relation.kind == "dimension_applies_to"
        }
        missing_dimensions = sorted(dimension_ids - related_dimensions)
        if missing_dimensions:
            raise ValueError(
                f"dimensions have no geometry relations: {missing_dimensions}"
            )
        if self.scale_mm_per_px is not None and self.scale_source is None:
            raise ValueError("known graph scale requires scale_source")
        return self

    def _validate_region(self, owner: str, region: SourceRegion) -> None:
        if not (
            0 <= region.x0 < region.x1 <= self.source.image_width
            and 0 <= region.y0 < region.y1 <= self.source.image_height
        ):
            raise ValueError(f"{owner} has an out-of-sheet source region")

    @staticmethod
    def _validate_evidence_refs(
        owner: str, refs: list[str], known_evidence: set[str]
    ) -> None:
        missing = sorted(set(refs) - known_evidence)
        if missing:
            raise ValueError(f"{owner} references missing evidence: {missing}")

    def _validate_entity_bounds(self, entity: Entity) -> None:
        points: list[Point]
        if isinstance(entity, Segment):
            points = [entity.p1, entity.p2]
        elif isinstance(entity, (Circle, Arc)):
            points = [
                Point(x=entity.center.x - entity.radius, y=entity.center.y - entity.radius),
                Point(x=entity.center.x + entity.radius, y=entity.center.y + entity.radius),
            ]
        elif isinstance(entity, Polyline):
            points = entity.points
        elif isinstance(entity, TextEntity):
            points = [entity.position]
        elif isinstance(entity, DimensionEntity):
            points = [entity.p1, entity.p2]
        elif isinstance(entity, HatchRegion):
            points = [*entity.boundary, *(point for hole in entity.holes for point in hole)]
        elif isinstance(entity, AnnotationEntity):
            points = [entity.position, *([entity.leader] if entity.leader else [])]
        else:  # pragma: no cover - discriminated Entity union is exhaustive
            raise ValueError(f"unsupported graph entity type: {entity.type}")
        if any(
            point.x < 0
            or point.y < 0
            or point.x > self.source.image_width
            or point.y > self.source.image_height
            for point in points
        ):
            raise ValueError(f"entity {entity.id} lies outside the source sheet")

    def content_sha256(self) -> str:
        payload = self.model_dump_json(exclude={"reader_manifest"})
        return hashlib.sha256(payload.encode()).hexdigest()


class DrawingGraphDraftError(ValueError):
    """The graph is structurally valid but not complete enough to redraw."""


class DrawingGraphVerificationIssue(BaseModel):
    code: str
    severity: Literal["error", "warning"]
    message: str
    entity_ids: list[str] = Field(default_factory=list)


class DrawingGraphVerification(BaseModel):
    contract: Literal["drawing-graph-verifier-v1"] = "drawing-graph-verifier-v1"
    issues: list[DrawingGraphVerificationIssue] = Field(default_factory=list)
    entity_evidence_rate: float = Field(ge=0.0, le=1.0)
    relation_evidence_rate: float = Field(ge=0.0, le=1.0)
    dimensions_checked: int = 0
    dimensions_consistent: int = 0
    pixel_recall: float | None = None
    pixel_precision: float | None = None
    draft_ready: bool = False
    exact_ready: bool = False

    @property
    def blocking(self) -> list[DrawingGraphVerificationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]


def _measured_dimension_value(
    dimension: DimensionEntity, target: Entity, scale: float
) -> float | None:
    if dimension.kind == "linear" and isinstance(target, Segment):
        return math.hypot(
            target.p2.x - target.p1.x, target.p2.y - target.p1.y
        ) * scale
    if dimension.kind == "diameter" and isinstance(target, Circle):
        return 2.0 * target.radius * scale
    if dimension.kind == "radial" and isinstance(target, (Circle, Arc)):
        return target.radius * scale
    return None


def verify_drawing_graph(
    graph: EngineeringDrawingGraph,
    *,
    pixel_recall: float | None = None,
    pixel_precision: float | None = None,
) -> DrawingGraphVerification:
    """Independently check completeness and dimension-to-geometry consistency."""
    issues: list[DrawingGraphVerificationIssue] = []
    active_unresolved = [
        region.id for region in graph.unresolved_regions if not region.resolved
    ]
    if active_unresolved:
        issues.append(DrawingGraphVerificationIssue(
            code="GRAPH_UNRESOLVED",
            severity="error",
            message=f"Unresolved source regions: {len(active_unresolved)}",
        ))
    if graph.scale_mm_per_px is None:
        issues.append(DrawingGraphVerificationIssue(
            code="GRAPH_SCALE_UNKNOWN",
            severity="error",
            message="Metric scale is required to verify drawing dimensions",
        ))
    if pixel_recall is None or pixel_precision is None:
        issues.append(DrawingGraphVerificationIssue(
            code="GRAPH_PIXEL_CHECK_MISSING",
            severity="error",
            message="Independent source-pixel verification has not run",
        ))
    elif pixel_recall < 0.995 or pixel_precision < 0.995:
        issues.append(DrawingGraphVerificationIssue(
            code="GRAPH_PIXEL_GATE_FAILED",
            severity="error",
            message=(
                f"Pixel gate failed: recall={pixel_recall:.4f}, "
                f"precision={pixel_precision:.4f}"
            ),
        ))

    entities = {entity.id: entity for entity in graph.entities}
    dimension_relations = {
        relation.source_entity_id: relation
        for relation in graph.relations
        if relation.kind == "dimension_applies_to"
    }
    annotation_relations = {
        relation.source_entity_id
        for relation in graph.relations
        if relation.kind == "annotation_applies_to"
    }
    dimensions_checked = 0
    dimensions_consistent = 0
    for entity in graph.entities:
        if isinstance(entity, AnnotationEntity) and entity.id not in annotation_relations:
            issues.append(DrawingGraphVerificationIssue(
                code="GRAPH_ANNOTATION_TARGET_MISSING",
                severity="error",
                message=f"Annotation {entity.id} is not linked to geometry",
                entity_ids=[entity.id],
            ))
        if not isinstance(entity, DimensionEntity):
            continue
        relation = dimension_relations[entity.id]
        target = entities[relation.target_entity_ids[0]]
        if graph.scale_mm_per_px is None or entity.value_mm is None:
            issues.append(DrawingGraphVerificationIssue(
                code="GRAPH_DIMENSION_VALUE_MISSING",
                severity="error",
                message=f"Dimension {entity.id} has no verifiable metric value",
                entity_ids=[entity.id, target.id],
            ))
            continue
        measured = _measured_dimension_value(entity, target, graph.scale_mm_per_px)
        if measured is None:
            issues.append(DrawingGraphVerificationIssue(
                code="GRAPH_DIMENSION_TARGET_UNSUPPORTED",
                severity="error",
                message=f"Dimension {entity.id} target cannot be measured",
                entity_ids=[entity.id, target.id],
            ))
            continue
        dimensions_checked += 1
        allowed = max(0.02, abs(entity.value_mm) * 0.005)
        if abs(measured - entity.value_mm) > allowed:
            issues.append(DrawingGraphVerificationIssue(
                code="GRAPH_DIMENSION_MISMATCH",
                severity="error",
                message=(
                    f"Dimension {entity.id}: stated={entity.value_mm:g} mm, "
                    f"geometry={measured:g} mm"
                ),
                entity_ids=[entity.id, target.id],
            ))
        else:
            dimensions_consistent += 1

    entity_evidence_rate = sum(bool(entity.evidence) for entity in graph.entities) / len(
        graph.entities
    )
    relation_evidence_rate = (
        sum(bool(relation.evidence) for relation in graph.relations)
        / len(graph.relations)
        if graph.relations
        else 1.0
    )
    blocking = any(issue.severity == "error" for issue in issues)
    exact_assurance = all(
        entity.assurance in ("constraint_validated", "human_approved")
        for entity in graph.entities
    ) and all(
        relation.assurance in ("constraint_validated", "human_approved")
        for relation in graph.relations
    )
    return DrawingGraphVerification(
        issues=issues,
        entity_evidence_rate=entity_evidence_rate,
        relation_evidence_rate=relation_evidence_rate,
        dimensions_checked=dimensions_checked,
        dimensions_consistent=dimensions_consistent,
        pixel_recall=pixel_recall,
        pixel_precision=pixel_precision,
        draft_ready=not blocking,
        exact_ready=(
            not blocking
            and graph.graph_status in ("verified", "human_reviewed")
            and exact_assurance
        ),
    )


def draft_drawing_graph(
    graph: EngineeringDrawingGraph,
    *,
    verification: DrawingGraphVerification | None = None,
) -> CadIR:
    """Copy a complete graph into CadIR without interpretation or ID changes."""
    blocking = [region.id for region in graph.unresolved_regions if not region.resolved]
    if blocking:
        raise DrawingGraphDraftError(
            f"drawing graph contains unresolved regions: {sorted(blocking)}"
        )
    entities = [entity.model_copy(deep=True) for entity in graph.entities]
    evidence_regions = {item.id: item.region for item in graph.evidence}
    for entity in entities:
        # Canonical provenance projection only: geometry is unchanged. CadIR
        # render verification consumes source_region directly, while the graph
        # stores normalized evidence objects by stable id.
        if entity.source_region is None and entity.evidence:
            entity.source_region = evidence_regions[entity.evidence[0]].model_copy()
    relations = [
        CadEntityRelation(
            id=relation.id,
            kind=relation.kind,
            source_entity_id=relation.source_entity_id,
            target_entity_ids=list(relation.target_entity_ids),
            parameters=dict(relation.parameters),
            confidence=relation.confidence,
            assurance=relation.assurance,
            evidence=list(relation.evidence),
        )
        for relation in graph.relations
    ]
    exact = bool(verification and verification.exact_ready)
    source_kind = "scan" if graph.source.kind == "pdf_page" else graph.source.kind
    return CadIR(
        source=SourceInfo(
            image_width=graph.source.image_width,
            image_height=graph.source.image_height,
            kind=source_kind,
        ),
        scale=graph.scale_mm_per_px,
        scale_source=graph.scale_source,
        sheet=graph.sheet.model_copy(deep=True),
        entities=entities,
        relations=relations,
        unresolved_regions=[region.model_copy(deep=True) for region in graph.unresolved_regions],
        digitization_status="exact_candidate" if exact else "review_required",
        recognizer_used="drawing-graph-drafter-v1",
    )


_DRAWING_GRAPH_PROMPT = """Ты — координатный reader технического чертежа.
Твоя единственная задача — вернуть полный EngineeringDrawingGraph JSON в
ГЛОБАЛЬНЫХ пиксельных координатах исходного листа. Не перечерчивай, не
исправляй линии и не угадывай отсутствующее.

Обязательные корневые поля:
schema_version=1, graph_status="reader_output", source, scale_mm_per_px,
scale_source, sheet, evidence[], views[], entities[], relations[],
unresolved_regions[], reader_manifest.

entities[] используют CadIR-типы:
- segment: id,type,p1,p2,line_class,width_class,confidence,origin,assurance,evidence;
- circle: center,radius; arc: center,radius,start_angle,end_angle;
- polyline: points,closed; text: position,text,height,rotation;
- dimension: kind,p1,p2,text,value_mm,tolerance;
- hatch: boundary,holes,pattern;
- annotation: kind,position,text,value,symbol,datum_refs,leader,height.

Для КАЖДОЙ не-human сущности обязателен evidence id. Evidence содержит kind,
глобальный source bbox, raw_text/model_key при наличии и confidence. Каждая
сущность принадлежит РОВНО одному view.entity_ids. Каждый dimension обязан
иметь relation kind="dimension_applies_to" со ссылками на измеряемые сущности;
annotation — annotation_applies_to. Добавляй также connected/coincident,
parallel/perpendicular/tangent/concentric/equal и same_feature_across_views,
когда связь видна. reader не имеет права ставить assurance выше observed или
inferred. Любые видимые, но неописанные области добавляй в unresolved_regions.
Если полный лист нельзя описать без пропусков, НЕ скрывай это. Только JSON.
"""


def _parse_graph_json(raw: str) -> dict:
    import json
    import re

    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE)
    start, end = text.find("{"), text.rfind("}")
    if not (0 <= start < end):
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


async def read_drawing_graph(
    image_bytes: bytes,
    *,
    router: Any | None = None,
    confidential: bool = True,
    source_kind: Literal["scan", "photo", "pdf_page", "import"] = "scan",
    page_index: int = 0,
) -> EngineeringDrawingGraph | None:
    """Read a full source sheet into the strict graph contract or fail closed."""
    import base64
    import io

    from PIL import Image

    from app.ai.cad_recognize.spec_vectorize import _spec_images
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    images, tile_descriptions = _spec_images(image)
    request = AIRequest(
        task=AITask.CAD_DRAWING_GRAPH_READ,
        messages=[ChatMessage(
            role="user",
            content=(
                _DRAWING_GRAPH_PROMPT
                + "\nКАРТА ИЗОБРАЖЕНИЙ:\n"
                + "\n".join(tile_descriptions)
                + f"\nРАЗМЕР ПОЛНОГО ЛИСТА: {image.width}×{image.height}px"
            ),
        )],
        images=[base64.b64encode(value).decode() for value in images],
        confidential=confidential,
        allow_cloud=False,
        thinking=False,
        metadata={"contract": "engineering-drawing-graph-v1"},
    )
    try:
        response = await router.run(request)
    except Exception:  # noqa: BLE001
        return None
    payload = _parse_graph_json(response.text or "")
    if not payload:
        return None
    payload["schema_version"] = 1
    payload["graph_status"] = "reader_output"
    payload["source"] = {
        "image_width": image.width,
        "image_height": image.height,
        "kind": source_kind,
        "page_index": page_index,
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
    }
    payload["reader_manifest"] = {
        "task": AITask.CAD_DRAWING_GRAPH_READ.value,
        "provider": response.provider.value,
        "model": response.model,
        "contract": "engineering-drawing-graph-v1",
    }
    try:
        return EngineeringDrawingGraph.model_validate(payload)
    except ValueError:
        return None
