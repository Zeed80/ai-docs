"""Strict source drawing graph and interpretation-free CadIR drafter.

The graph is the contract between coordinate recognition and redrawing. It
contains observations, stable identifiers and semantic relations for the full
sheet. The drafter below performs a one-to-one copy; it never recognizes,
repairs or invents geometry.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
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


class DrawingGraphReadAttempt(BaseModel):
    """Auditable reader result, including output rejected by the graph schema."""

    contract: Literal["engineering-drawing-graph-read-attempt-v1"] = (
        "engineering-drawing-graph-read-attempt-v1"
    )
    graph: EngineeringDrawingGraph | None = None
    raw_text: str = ""
    raw_sha256: str
    parsed_payload: dict[str, Any] | None = None
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)
    reader_manifest: dict[str, Any] = Field(default_factory=dict)
    stage_attempts: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.graph is not None


class DrawingGraphLayout(BaseModel):
    """Compact overview result. Geometry is deliberately forbidden here."""

    sheet: SheetInfo = Field(default_factory=SheetInfo)
    scale_mm_per_px: float | None = Field(default=None, gt=0)
    scale_source: Literal["manual", "calibration", "dpi", "sheet_format"] | None = None
    views: list[DrawingGraphView] = Field(min_length=1)
    unresolved_regions: list[UnresolvedRegion] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _discard_empty_unresolved_placeholders(cls, value: Any) -> Any:
        """Accept a VLM's empty-string sentinel without hiding real content."""
        if not isinstance(value, dict):
            return value
        unresolved = value.get("unresolved_regions")
        if not isinstance(unresolved, list):
            return value
        cleaned = [
            item
            for item in unresolved
            if not (isinstance(item, str) and not item.strip())
        ]
        if cleaned == unresolved:
            return value
        normalized = dict(value)
        normalized["unresolved_regions"] = cleaned
        return normalized

    @model_validator(mode="after")
    def _layout_has_unique_empty_views(self) -> "DrawingGraphLayout":
        ids = [view.id for view in self.views]
        if len(ids) != len(set(ids)):
            raise ValueError("layout view ids must be unique")
        if any(view.entity_ids for view in self.views):
            raise ValueError("layout stage cannot assign entities")
        if self.scale_mm_per_px is not None and self.scale_source is None:
            raise ValueError("layout scale requires scale_source")
        return self


class DrawingGraphFragmentEntity(BaseModel):
    view_id: str
    entity: Entity


class DrawingGraphFragment(BaseModel):
    tile_id: str
    source_region: SourceRegion
    ownership_region: SourceRegion
    evidence: list[DrawingGraphEvidence] = Field(default_factory=list)
    entities: list[DrawingGraphFragmentEntity] = Field(default_factory=list)
    relations: list[DrawingGraphRelation] = Field(default_factory=list)
    unresolved_regions: list[UnresolvedRegion] = Field(default_factory=list)


@dataclass(frozen=True)
class DrawingGraphTile:
    tile_id: str
    image_bytes: bytes
    source_region: SourceRegion
    ownership_region: SourceRegion


class VlmEvidenceCheck(BaseModel):
    entity_id: str
    entity_type: Literal["text", "dimension", "annotation"]
    evidence_id: str
    region: SourceRegion
    expected: dict[str, str | float | None]
    observed: dict[str, str | float | bool | None] = Field(default_factory=dict)
    exact_match: bool = False
    model: str | None = None
    provider: str | None = None
    raw_sha256: str | None = None
    error: str | None = None


class VlmGraphEvidenceReport(BaseModel):
    contract: Literal["vlm-graph-evidence-verifier-v1"] = (
        "vlm-graph-evidence-verifier-v1"
    )
    task: Literal["cad_drawing_graph_evidence_verify"] = (
        "cad_drawing_graph_evidence_verify"
    )
    classic_ocr_used: Literal[False] = False
    reader_model: str | None = None
    verifier_models: list[str] = Field(default_factory=list)
    checks: list[VlmEvidenceCheck] = Field(default_factory=list)
    expected_checks: int = 0
    exact_checks: int = 0
    complete: bool = False
    independent: bool = False

    @property
    def blocking(self) -> bool:
        return not self.complete or not self.independent


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
    vlm_evidence: VlmGraphEvidenceReport | None = None,
    require_vlm_evidence: bool = False,
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
    if require_vlm_evidence and vlm_evidence is None:
        issues.append(DrawingGraphVerificationIssue(
            code="GRAPH_VLM_EVIDENCE_MISSING",
            severity="error",
            message="Independent crop-level VLM evidence verification has not run",
        ))
    elif vlm_evidence is not None and vlm_evidence.blocking:
        failed_ids = [
            check.entity_id for check in vlm_evidence.checks if not check.exact_match
        ]
        issues.append(DrawingGraphVerificationIssue(
            code=(
                "GRAPH_VLM_VERIFIER_NOT_INDEPENDENT"
                if not vlm_evidence.independent
                else "GRAPH_VLM_EVIDENCE_MISMATCH"
            ),
            severity="error",
            message=(
                "Crop-level VLM verification is not independent from the reader"
                if not vlm_evidence.independent
                else (
                    "Crop-level VLM verification failed for "
                    f"{len(failed_ids)} text/symbol entities"
                )
            ),
            entity_ids=failed_ids,
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


def _textual_expectation(
    entity: TextEntity | DimensionEntity | AnnotationEntity,
) -> dict[str, str | float | None]:
    if isinstance(entity, TextEntity):
        return {"text": entity.text}
    if isinstance(entity, DimensionEntity):
        return {
            "text": entity.text,
            "value_mm": entity.value_mm,
            "tolerance": entity.tolerance,
        }
    return {
        "text": entity.text,
        "value": entity.value,
        "symbol": entity.symbol,
    }


def _vlm_observation_matches(
    expected: dict[str, str | float | None], observed: dict[str, Any]
) -> bool:
    if observed.get("visible") is not True:
        return False
    for key, value in expected.items():
        if value is None:
            continue
        actual = observed.get(key)
        if isinstance(value, float):
            try:
                if abs(float(actual) - value) > 1e-6:
                    return False
            except (TypeError, ValueError):
                return False
        elif actual != value:
            return False
    return True


_VLM_EVIDENCE_PROMPT = """Ты — независимый VLM-проверяющий фрагмента технического чертежа.
Прочитай только видимое содержимое crop без догадок и без исправлений.
Верни один JSON:
{"visible":bool,"entity_type":"text|dimension|annotation","text":string|null,
 "value_mm":number|null,"tolerance":string|null,"value":string|null,
 "symbol":string|null,"confidence":number}
Сохраняй регистр, знаки диаметра/радиуса, запятые, точки, ±, индексы и пробелы
точно как на изображении. Не используй сведения из expected как ответ: expected
передан только для указания полей, которые надо независимо прочитать. Только JSON.
"""


_LAYOUT_PROMPT = """Ты — VLM стадии layout технического чертежа.
По обзорному изображению верни компактный JSON СТРОГО по схеме:
{"sheet":{"format":string|null,"width_mm":number|null,"height_mm":number|null,
 "frame":bool,"title_block":{},"frame_px":[x,y,w,h]|null},
 "scale_mm_per_px":number|null,"scale_source":"manual|calibration|dpi|sheet_format"|null,
 "views":[{"id":string,"kind":"sheet|front|top|side|section|detail|title_block|table|unknown",
 "region":{"x0":number,"y0":number,"x1":number,"y1":number},
 "entity_ids":[],"parent_view_id":string|null,"label":string|null,
 "confidence":number,"evidence":[]}],
 "unresolved_regions":[{"id":string,
 "region":{"x0":number,"y0":number,"x1":number,"y1":number},
 "reason":"unvectorized_ink|ocr_unresolved|recognizer_disagreement|unsupported_content",
 "ink_pixels":0,"resolved":false}]}
Координаты views — глобальные пиксели полного листа. На этой стадии запрещено
возвращать entities, evidence геометрии и relations. Не угадывай масштаб.
Каждый вид, разрез, таблица и основная надпись должны иметь отдельный bbox.
Если неизвестных областей нет, unresolved_regions ДОЛЖЕН быть ровно [].
Никогда не помещай строки или null в unresolved_regions.
Только один завершённый JSON.
"""


_FRAGMENT_PROMPT = """Ты — VLM стадии source-resolution graph fragment.
Верни один завершённый JSON:
{"tile_id":string,
 "source_region":{"x0":number,"y0":number,"x1":number,"y1":number},
 "ownership_region":{"x0":number,"y0":number,"x1":number,"y1":number},
 "evidence":[{"id":string,"kind":"pixel_support|ocr|geometry_detector|symbol_detector|relation_model|constraint_check|human",
 "region":{"x0":number,"y0":number,"x1":number,"y1":number},
 "image_index":0,"raw_text":string|null,"model_key":string|null,"confidence":number}],
 "entities":[{"view_id":string,"entity":CadIR-entity}],
 "relations":[...],"unresolved_regions":[...]}

Извлекай только сущности, чья опорная точка находится ВНУТРИ ownership_region.
Crop может включать overlap вне ownership_region только как контекст. Все
координаты entities/evidence/relations — ГЛОБАЛЬНЫЕ пиксели полного листа.
ID каждого evidence/entity/relation должен начинаться с указанного tile_id и
ДВОЕТОЧИЯ, например tile-00-00:ev-1. Поле bbox_2d запрещено: используй region.
Поле text_content запрещено: используй raw_text. Один видимый объект — ровно
одна evidence и одна entity; дубли с одинаковым region запрещены.
Не повторяй сущности из overlap. Для text сохраняй строку дословно.
Для dimension обязательны kind linear|diameter|radial|angular, p1/p2, text,
value_mm/tolerance и dimension_applies_to. Для annotation обязательна
annotation_applies_to. Не видимое или обрезанное добавляй в unresolved_regions.
Классические OCR не используются. Только JSON без пояснений.
"""


def _tile_origins(length: int, tile_size: int, overlap: int) -> list[int]:
    if length <= tile_size:
        return [0]
    step = tile_size - overlap
    values = list(range(0, length - tile_size + 1, step))
    last = length - tile_size
    if values[-1] != last:
        values.append(last)
    return values


def _ownership_bounds(origins: list[int], length: int, tile_size: int) -> list[tuple[int, int]]:
    centers = [origin + min(tile_size, length - origin) / 2 for origin in origins]
    boundaries = [0]
    boundaries.extend(round((left + right) / 2) for left, right in zip(centers, centers[1:]))
    boundaries.append(length)
    return list(zip(boundaries, boundaries[1:]))


def build_drawing_graph_tiles(
    image: Any,
    *,
    tile_size: int = 1000,
    overlap: int = 120,
    max_tiles: int = 16,
) -> list[DrawingGraphTile]:
    """Build overlapping crops with non-overlapping deterministic ownership."""
    import io

    xs = _tile_origins(image.width, tile_size, overlap)
    ys = _tile_origins(image.height, tile_size, overlap)
    if len(xs) * len(ys) > max_tiles:
        raise ValueError(
            f"sheet requires {len(xs) * len(ys)} tiles, limit is {max_tiles}"
        )
    x_ownership = _ownership_bounds(xs, image.width, tile_size)
    y_ownership = _ownership_bounds(ys, image.height, tile_size)
    tiles: list[DrawingGraphTile] = []
    for row, y in enumerate(ys):
        for column, x in enumerate(xs):
            x1 = min(x + tile_size, image.width)
            y1 = min(y + tile_size, image.height)
            buffer = io.BytesIO()
            image.crop((x, y, x1, y1)).save(buffer, format="PNG")
            tiles.append(DrawingGraphTile(
                tile_id=f"tile-{row:02d}-{column:02d}",
                image_bytes=buffer.getvalue(),
                source_region=SourceRegion(x0=x, y0=y, x1=x1, y1=y1),
                ownership_region=SourceRegion(
                    x0=x_ownership[column][0],
                    y0=y_ownership[row][0],
                    x1=x_ownership[column][1],
                    y1=y_ownership[row][1],
                ),
            ))
    return tiles


def _entity_anchor(entity: Entity) -> Point:
    if isinstance(entity, Segment):
        return Point(x=(entity.p1.x + entity.p2.x) / 2, y=(entity.p1.y + entity.p2.y) / 2)
    if isinstance(entity, (Circle, Arc)):
        return entity.center
    if isinstance(entity, Polyline):
        return entity.points[len(entity.points) // 2]
    if isinstance(entity, TextEntity):
        return entity.position
    if isinstance(entity, DimensionEntity):
        return Point(x=(entity.p1.x + entity.p2.x) / 2, y=(entity.p1.y + entity.p2.y) / 2)
    if isinstance(entity, HatchRegion):
        return Point(
            x=sum(point.x for point in entity.boundary) / len(entity.boundary),
            y=sum(point.y for point in entity.boundary) / len(entity.boundary),
        )
    return entity.position


def _inside(region: SourceRegion, point: Point) -> bool:
    return region.x0 <= point.x < region.x1 and region.y0 <= point.y < region.y1


def assemble_drawing_graph_fragments(
    *,
    source: DrawingGraphSource,
    layout: DrawingGraphLayout,
    fragments: list[DrawingGraphFragment],
    reader_manifest: dict[str, Any],
) -> EngineeringDrawingGraph:
    """Deterministically assemble validated bounded fragments without guessing."""
    views = {view.id: view.model_copy(deep=True) for view in layout.views}
    evidence: list[DrawingGraphEvidence] = []
    entities: list[Entity] = []
    relations: list[DrawingGraphRelation] = []
    unresolved = [item.model_copy(deep=True) for item in layout.unresolved_regions]
    seen_ids: set[str] = set()
    for fragment in fragments:
        prefix = fragment.tile_id + ":"
        for collection in (fragment.evidence, fragment.relations):
            for item in collection:
                if not item.id.startswith(prefix):
                    raise ValueError(f"{item.id} does not use tile prefix {prefix}")
                if item.id in seen_ids:
                    raise ValueError(f"duplicate staged graph id: {item.id}")
                seen_ids.add(item.id)
        evidence.extend(item.model_copy(deep=True) for item in fragment.evidence)
        relations.extend(item.model_copy(deep=True) for item in fragment.relations)
        for observation in fragment.entities:
            entity = observation.entity
            if not entity.id.startswith(prefix):
                raise ValueError(f"{entity.id} does not use tile prefix {prefix}")
            if entity.id in seen_ids:
                raise ValueError(f"duplicate staged graph id: {entity.id}")
            if observation.view_id not in views:
                raise ValueError(
                    f"fragment {fragment.tile_id} references unknown view {observation.view_id}"
                )
            if not _inside(fragment.ownership_region, _entity_anchor(entity)):
                raise ValueError(
                    f"entity {entity.id} anchor lies outside tile ownership region"
                )
            seen_ids.add(entity.id)
            entities.append(entity.model_copy(deep=True))
            views[observation.view_id].entity_ids.append(entity.id)
        unresolved.extend(item.model_copy(deep=True) for item in fragment.unresolved_regions)
    if not entities:
        raise ValueError("staged graph contains no entities")
    return EngineeringDrawingGraph(
        source=source,
        scale_mm_per_px=layout.scale_mm_per_px,
        scale_source=layout.scale_source,
        sheet=layout.sheet.model_copy(deep=True),
        evidence=evidence,
        views=list(views.values()),
        entities=entities,
        relations=relations,
        unresolved_regions=unresolved,
        reader_manifest=reader_manifest,
    )


async def verify_graph_evidence_with_vlm(
    image_bytes: bytes,
    graph: EngineeringDrawingGraph,
    *,
    router: Any | None = None,
    max_checks: int = 256,
) -> VlmGraphEvidenceReport:
    """Verify every text/dimension/annotation crop with an independent VLM."""
    import base64
    import io

    from PIL import Image

    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    evidence = {item.id: item for item in graph.evidence}
    textual = [
        entity
        for entity in graph.entities
        if isinstance(entity, (TextEntity, DimensionEntity, AnnotationEntity))
    ]
    checks: list[VlmEvidenceCheck] = []
    verifier_models: list[str] = []
    reader_model = str(graph.reader_manifest.get("model") or "") or None
    reader_models = {
        str(model) for model in (graph.reader_manifest.get("models") or []) if model
    }
    if reader_model and not reader_models:
        reader_models.add(reader_model)
    for entity in textual[:max_checks]:
        evidence_id = entity.evidence[0]
        item = evidence[evidence_id]
        region = item.region
        pad = max(8, int(max(region.x1 - region.x0, region.y1 - region.y0) * 0.08))
        crop_box = (
            max(0, region.x0 - pad),
            max(0, region.y0 - pad),
            min(image.width, region.x1 + pad),
            min(image.height, region.y1 + pad),
        )
        crop = image.crop(crop_box)
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        expected = _textual_expectation(entity)
        check = VlmEvidenceCheck(
            entity_id=entity.id,
            entity_type=entity.type,
            evidence_id=evidence_id,
            region=region,
            expected=expected,
        )
        request = AIRequest(
            task=AITask.CAD_DRAWING_GRAPH_EVIDENCE_VERIFY,
            messages=[ChatMessage(
                role="user",
                content=(
                    _VLM_EVIDENCE_PROMPT
                    + "\nТИП ПРОВЕРКИ: "
                    + entity.type
                    + "\nПОЛЯ: "
                    + ", ".join(expected)
                ),
            )],
            images=[base64.b64encode(buffer.getvalue()).decode()],
            confidential=True,
            allow_cloud=False,
            thinking=False,
            metadata={
                "contract": "vlm-graph-evidence-verifier-v1",
                "num_predict": 1024,
                "entity_id": entity.id,
                "evidence_id": evidence_id,
            },
        )
        try:
            response = await router.run(request)
            raw = response.text or ""
            observed = _parse_graph_json(raw)
            model = response.model
            provider = response.provider.value
            check.model = model
            check.provider = provider
            check.raw_sha256 = hashlib.sha256(raw.encode()).hexdigest()
            check.observed = {
                key: value
                for key, value in observed.items()
                if key in {
                    "visible", "entity_type", "text", "value_mm",
                    "tolerance", "value", "symbol", "confidence",
                }
            }
            check.exact_match = (
                observed.get("entity_type") == entity.type
                and _vlm_observation_matches(expected, observed)
            )
            if model not in verifier_models:
                verifier_models.append(model)
        except Exception as exc:  # noqa: BLE001
            check.error = str(exc)[:500]
        checks.append(check)
    exact_checks = sum(check.exact_match for check in checks)
    complete = len(textual) <= max_checks and len(checks) == len(textual)
    complete = complete and exact_checks == len(textual)
    independent = not textual or (
        bool(verifier_models)
        and all(model not in reader_models for model in verifier_models)
    )
    return VlmGraphEvidenceReport(
        reader_model=reader_model,
        verifier_models=verifier_models,
        checks=checks,
        expected_checks=len(textual),
        exact_checks=exact_checks,
        complete=complete,
        independent=independent,
    )


async def read_drawing_graph_attempt(
    image_bytes: bytes,
    *,
    router: Any | None = None,
    confidential: bool = True,
    source_kind: Literal["scan", "photo", "pdf_page", "import"] = "scan",
    page_index: int = 0,
) -> DrawingGraphReadAttempt:
    """Read a sheet and retain diagnostics even when strict validation fails."""
    import base64
    import io

    from PIL import Image

    from app.ai.cad_recognize.spec_vectorize import _spec_images
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        return DrawingGraphReadAttempt(
            raw_sha256=hashlib.sha256(b"").hexdigest(),
            validation_errors=[{
                "type": "source_image_invalid",
                "msg": str(exc)[:500],
            }],
        )
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
    except Exception as exc:  # noqa: BLE001
        return DrawingGraphReadAttempt(
            raw_sha256=hashlib.sha256(b"").hexdigest(),
            validation_errors=[{
                "type": "reader_call_failed",
                "msg": str(exc)[:500],
            }],
        )
    raw = response.text or ""
    raw_sha256 = hashlib.sha256(raw.encode()).hexdigest()
    payload = _parse_graph_json(raw)
    reader_manifest = {
        "task": AITask.CAD_DRAWING_GRAPH_READ.value,
        "provider": response.provider.value,
        "model": response.model,
        "contract": "engineering-drawing-graph-v1",
    }
    if not payload:
        return DrawingGraphReadAttempt(
            raw_text=raw,
            raw_sha256=raw_sha256,
            validation_errors=[{
                "type": "reader_json_invalid",
                "msg": "Reader output does not contain one valid JSON object",
            }],
            reader_manifest=reader_manifest,
        )
    payload["schema_version"] = 1
    payload["graph_status"] = "reader_output"
    payload["source"] = {
        "image_width": image.width,
        "image_height": image.height,
        "kind": source_kind,
        "page_index": page_index,
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
    }
    payload["reader_manifest"] = reader_manifest
    try:
        graph = EngineeringDrawingGraph.model_validate(payload)
        return DrawingGraphReadAttempt(
            graph=graph,
            raw_text=raw,
            raw_sha256=raw_sha256,
            parsed_payload=payload,
            reader_manifest=reader_manifest,
        )
    except ValueError as exc:
        if hasattr(exc, "json"):
            import json

            validation_errors = json.loads(
                exc.json(include_url=False, include_input=False)
            )
        else:
            validation_errors = [
                {"type": "graph_validation_failed", "msg": str(exc)[:1000]}
            ]
        return DrawingGraphReadAttempt(
            raw_text=raw,
            raw_sha256=raw_sha256,
            parsed_payload=payload,
            validation_errors=validation_errors,
            reader_manifest=reader_manifest,
        )


def _validation_errors(exc: Exception, *, stage: str) -> list[dict[str, Any]]:
    if hasattr(exc, "json"):
        import json

        errors = json.loads(exc.json(include_url=False, include_input=False))
    else:
        errors = [{"type": "stage_validation_failed", "msg": str(exc)[:1000]}]
    for error in errors:
        error["loc"] = [stage, *list(error.get("loc") or [])]
    return errors


async def read_drawing_graph_staged_attempt(
    image_bytes: bytes,
    *,
    router: Any | None = None,
    confidential: bool = True,
    source_kind: Literal["scan", "photo", "pdf_page", "import"] = "scan",
    page_index: int = 0,
) -> DrawingGraphReadAttempt:
    """Read layout and bounded source-resolution fragments in separate VLM calls."""
    import base64
    import io
    import json

    from PIL import Image

    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        tiles = build_drawing_graph_tiles(image)
    except Exception as exc:  # noqa: BLE001
        errors = _validation_errors(exc, stage="source")
        return DrawingGraphReadAttempt(
            raw_sha256=hashlib.sha256(b"").hexdigest(),
            validation_errors=errors,
        )

    stage_attempts: list[dict[str, Any]] = []
    raw_parts: list[str] = []
    reader_models: list[str] = []

    overview = image.copy()
    overview.thumbnail((1600, 1600))
    overview_buffer = io.BytesIO()
    overview.save(overview_buffer, format="PNG")
    layout_request = AIRequest(
        task=AITask.CAD_DRAWING_GRAPH_LAYOUT,
        messages=[ChatMessage(
            role="user",
            content=(
                _LAYOUT_PROMPT
                + f"\nПОЛНЫЙ ЛИСТ: {image.width}x{image.height}px. "
                + f"OVERVIEW: {overview.width}x{overview.height}px."
            ),
        )],
        images=[base64.b64encode(overview_buffer.getvalue()).decode()],
        confidential=confidential,
        allow_cloud=False,
        thinking=False,
        metadata={"contract": "drawing-graph-layout-v1", "num_predict": 2048},
    )
    layout_raw = ""
    try:
        response = await router.run(layout_request)
        layout_raw = response.text or ""
        raw_parts.append(layout_raw)
        parsed = _parse_graph_json(layout_raw)
        if not parsed:
            raise ValueError("layout output does not contain valid JSON")
        layout = DrawingGraphLayout.model_validate(parsed)
        reader_models.append(response.model)
        stage_attempts.append({
            "stage": "layout",
            "task": AITask.CAD_DRAWING_GRAPH_LAYOUT.value,
            "model": response.model,
            "provider": response.provider.value,
            "raw_sha256": hashlib.sha256(layout_raw.encode()).hexdigest(),
            "parsed_payload": parsed,
            "validation_errors": [],
        })
    except Exception as exc:  # noqa: BLE001
        errors = _validation_errors(exc, stage="layout")
        stage_attempts.append({
            "stage": "layout",
            "raw_sha256": hashlib.sha256(layout_raw.encode()).hexdigest(),
            "validation_errors": errors,
        })
        combined = "\n\n--- stage ---\n\n".join(raw_parts)
        return DrawingGraphReadAttempt(
            raw_text=combined,
            raw_sha256=hashlib.sha256(combined.encode()).hexdigest(),
            validation_errors=errors,
            stage_attempts=stage_attempts,
        )

    view_contract = [
        {
            "id": view.id,
            "kind": view.kind,
            "region": view.region.model_dump(mode="json"),
            "label": view.label,
        }
        for view in layout.views
    ]
    fragments: list[DrawingGraphFragment] = []
    for tile in tiles:
        prompt = (
            _FRAGMENT_PROMPT
            + "\nTILE_ID: "
            + tile.tile_id
            + "\nSOURCE_REGION: "
            + json.dumps(tile.source_region.model_dump(mode="json"))
            + "\nOWNERSHIP_REGION: "
            + json.dumps(tile.ownership_region.model_dump(mode="json"))
            + "\nKNOWN_VIEWS: "
            + json.dumps(view_contract, ensure_ascii=False)
        )
        request = AIRequest(
            task=AITask.CAD_DRAWING_GRAPH_FRAGMENT_READ,
            messages=[ChatMessage(role="user", content=prompt)],
            images=[base64.b64encode(tile.image_bytes).decode()],
            confidential=confidential,
            allow_cloud=False,
            thinking=False,
            metadata={
                "contract": "drawing-graph-fragment-v1",
                "num_predict": 6144,
                "tile_id": tile.tile_id,
                "source_region": tile.source_region.model_dump(mode="json"),
                "ownership_region": tile.ownership_region.model_dump(mode="json"),
            },
        )
        fragment_raw = ""
        try:
            response = await router.run(request)
            fragment_raw = response.text or ""
            raw_parts.append(fragment_raw)
            parsed = _parse_graph_json(fragment_raw)
            if not parsed:
                raise ValueError("fragment output does not contain valid JSON")
            parsed["tile_id"] = tile.tile_id
            parsed["source_region"] = tile.source_region.model_dump(mode="json")
            parsed["ownership_region"] = tile.ownership_region.model_dump(mode="json")
            fragment = DrawingGraphFragment.model_validate(parsed)
            for evidence in fragment.evidence:
                if not (
                    tile.source_region.x0 <= evidence.region.x0 < evidence.region.x1 <= tile.source_region.x1
                    and tile.source_region.y0 <= evidence.region.y0 < evidence.region.y1 <= tile.source_region.y1
                ):
                    raise ValueError(
                        f"evidence {evidence.id} lies outside tile source region"
                    )
            fragments.append(fragment)
            if response.model not in reader_models:
                reader_models.append(response.model)
            stage_attempts.append({
                "stage": "fragment",
                "tile_id": tile.tile_id,
                "task": AITask.CAD_DRAWING_GRAPH_FRAGMENT_READ.value,
                "model": response.model,
                "provider": response.provider.value,
                "raw_sha256": hashlib.sha256(fragment_raw.encode()).hexdigest(),
                "parsed_payload": parsed,
                "validation_errors": [],
            })
        except Exception as exc:  # noqa: BLE001
            errors = _validation_errors(exc, stage=f"fragment:{tile.tile_id}")
            stage_attempts.append({
                "stage": "fragment",
                "tile_id": tile.tile_id,
                "raw_sha256": hashlib.sha256(fragment_raw.encode()).hexdigest(),
                "validation_errors": errors,
            })
            combined = "\n\n--- stage ---\n\n".join(raw_parts)
            return DrawingGraphReadAttempt(
                raw_text=combined,
                raw_sha256=hashlib.sha256(combined.encode()).hexdigest(),
                validation_errors=errors,
                reader_manifest={"models": reader_models},
                stage_attempts=stage_attempts,
            )

    source = DrawingGraphSource(
        image_width=image.width,
        image_height=image.height,
        kind=source_kind,
        page_index=page_index,
        sha256=hashlib.sha256(image_bytes).hexdigest(),
    )
    reader_manifest = {
        "task": "cad_drawing_graph_staged_read",
        "model": " + ".join(reader_models),
        "models": reader_models,
        "contract": "engineering-drawing-graph-staged-v2",
        "layout_task": AITask.CAD_DRAWING_GRAPH_LAYOUT.value,
        "fragment_task": AITask.CAD_DRAWING_GRAPH_FRAGMENT_READ.value,
        "tiles": len(tiles),
    }
    combined = "\n\n--- stage ---\n\n".join(raw_parts)
    try:
        graph = assemble_drawing_graph_fragments(
            source=source,
            layout=layout,
            fragments=fragments,
            reader_manifest=reader_manifest,
        )
    except Exception as exc:  # noqa: BLE001
        errors = _validation_errors(exc, stage="assembly")
        return DrawingGraphReadAttempt(
            raw_text=combined,
            raw_sha256=hashlib.sha256(combined.encode()).hexdigest(),
            validation_errors=errors,
            reader_manifest=reader_manifest,
            stage_attempts=stage_attempts,
        )
    return DrawingGraphReadAttempt(
        graph=graph,
        raw_text=combined,
        raw_sha256=hashlib.sha256(combined.encode()).hexdigest(),
        parsed_payload=graph.model_dump(mode="json"),
        reader_manifest=reader_manifest,
        stage_attempts=stage_attempts,
    )


async def read_drawing_graph(
    image_bytes: bytes,
    *,
    router: Any | None = None,
    confidential: bool = True,
    source_kind: Literal["scan", "photo", "pdf_page", "import"] = "scan",
    page_index: int = 0,
) -> EngineeringDrawingGraph | None:
    """Compatibility wrapper returning only a fully valid strict graph."""
    attempt = await read_drawing_graph_attempt(
        image_bytes,
        router=router,
        confidential=confidential,
        source_kind=source_kind,
        page_index=page_index,
    )
    return attempt.graph
