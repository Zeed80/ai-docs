"""Pydantic schema of the CAD IR.

Coordinate convention: entity coordinates are in **source-image pixel space**
(y grows downward), because that is what recognition backends and the overlay
UI operate in. ``CadIR.scale`` (mm per px) plus the sheet origin convert to
drawing millimetres at render time; DXF render flips the y axis. When the
scale is unknown the IR is still valid — validators flag ``SCALE_UNKNOWN``
and exports are in conditional units until the user supplies a scale.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = 5

LineClass = Literal["contour", "axis", "dim", "hatch", "hidden", "thin"]
WidthClass = Literal["main", "thin"]
EntityOrigin = Literal["neural", "vlm", "cv", "human", "spec"]

# Assurance ladder — engineering trust is a state, not a score. Recognition
# backends may only produce the bottom rungs; solvers/cross-checks raise to
# *_validated; ONLY a human action reaches human_approved. Enforcement lives
# in assurance.py — the schema just names the states.
Assurance = Literal[
    "observed",              # прочитано непосредственно из источника
    "inferred",              # восстановлено моделью/эвристикой
    "constraint_validated",  # согласуется с геометрией/ограничениями
    "calculation_validated", # подтверждено расчётом
    "human_approved",        # утверждено человеком
]

# line_class → DXF/SVG layer name (single mapping used by every render)
LINE_CLASS_LAYERS: dict[str, str] = {
    "contour": "OBJECT",
    "thin": "OBJECT_THIN",
    "axis": "CENTER",
    "hidden": "HIDDEN",
    "dim": "DIM",
    "hatch": "HATCH",
}
TEXT_LAYER = "ANNOTATION"


def _entity_id() -> str:
    return uuid.uuid4().hex[:12]


def new_entity_id() -> str:
    """Public id generator for callers that need to mint an id outside
    entity construction (e.g. duplicating an entity via ``model_copy``,
    which keeps the source's id unless told otherwise)."""
    return _entity_id()


class Point(BaseModel):
    x: float
    y: float


class SourceRegion(BaseModel):
    """Where in the source image this entity was read from (px bbox)."""

    x0: float
    y0: float
    x1: float
    y1: float


class UnresolvedRegion(BaseModel):
    """Source area that did not become verified, exportable CAD entities."""

    id: str = Field(default_factory=_entity_id)
    region: SourceRegion
    reason: Literal[
        "unvectorized_ink",
        "ocr_unresolved",
        "recognizer_disagreement",
        "unsupported_content",
    ] = "unvectorized_ink"
    ink_pixels: int = Field(default=0, ge=0)
    resolved: bool = False


class Alternative(BaseModel):
    """A competing interpretation of the same observation (hypothesis).

    ``value`` is the textual reading for text/dimension hypotheses; geometric
    alternatives reference a full entity payload in ``entity``.
    """

    value: str | None = None
    entity: dict | None = None
    p: float = Field(ge=0.0, le=1.0)


class _EntityBase(BaseModel):
    id: str = Field(default_factory=_entity_id)
    line_class: LineClass = "contour"
    width_class: WidthClass = "main"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    origin: EntityOrigin = "cv"
    assurance: Assurance = "inferred"
    source_region: SourceRegion | None = None
    # A2: construction (auxiliary) geometry — a reference the user draws to
    # constrain/align real geometry against. Rendered faintly on the canvas but
    # excluded from the DXF/SVG export and from coverage, exactly like a CAD
    # construction line: it guides the drawing, it is not part of it.
    construction: bool = False
    # competing interpretations, highest-p first; resolved by cross-checks or
    # by the human in review — never silently by the model itself
    alternatives: list[Alternative] = Field(default_factory=list)
    # ids of observations/checks backing this entity (OCR result, coverage
    # score, solver run) — the evidence trail of the provenance layer
    evidence: list[str] = Field(default_factory=list)


class Segment(_EntityBase):
    type: Literal["segment"] = "segment"
    p1: Point
    p2: Point


class Arc(_EntityBase):
    type: Literal["arc"] = "arc"
    center: Point
    radius: float = Field(gt=0)
    # degrees, counter-clockwise in image space (y-down), like cv2/ezdxf math
    start_angle: float
    end_angle: float


class Circle(_EntityBase):
    type: Literal["circle"] = "circle"
    center: Point
    radius: float = Field(gt=0)


class Polyline(_EntityBase):
    type: Literal["polyline"] = "polyline"
    points: list[Point] = Field(min_length=2)
    closed: bool = False


class TextEntity(_EntityBase):
    type: Literal["text"] = "text"
    position: Point
    text: str
    height: float = Field(default=3.5, gt=0)
    rotation: float = 0.0
    line_class: LineClass = "dim"


class DimensionEntity(_EntityBase):
    type: Literal["dimension"] = "dimension"
    kind: Literal["linear", "diameter", "radial", "angular"] = "linear"
    p1: Point
    p2: Point
    # human-readable label as it appears on the sheet, e.g. "Ø40H7", "R8"
    text: str = ""
    # parsed nominal value in mm when known (VLM/human), None when unread
    value_mm: float | None = None
    tolerance: str | None = None
    line_class: LineClass = "dim"


class HatchRegion(_EntityBase):
    type: Literal["hatch"] = "hatch"
    boundary: list[Point] = Field(min_length=3)
    # Nested loops cut OUT of ``boundary`` (a section fill with a bolt hole
    # through it, etc.) — each inner list is itself a closed loop, same
    # convention as ``boundary``. Empty when the region has no holes.
    holes: list[list[Point]] = Field(default_factory=list)
    pattern: Literal["ansi31", "solid"] = "ansi31"
    line_class: LineClass = "hatch"
    width_class: WidthClass = "thin"


class AnnotationEntity(_EntityBase):
    """A structured ЕСКД annotation (C4): roughness, thread, geometric
    tolerance, datum or weld — first-class data, not free text over a stroke.

    ``kind`` selects the standard; ``value``/``symbol``/``datum_refs`` hold the
    parsed payload; ``text`` is the canonical display string
    (``annotation_text`` builds it). ``leader`` optionally anchors the symbol
    to the feature it annotates."""

    type: Literal["annotation"] = "annotation"
    kind: Literal["roughness", "thread", "tolerance", "datum", "weld"]
    position: Point
    text: str = ""
    # roughness Ra/Rz value; thread designation "M20×1.5"; tolerance value mm
    value: str | None = None
    # tolerance geometric symbol ("flatness"/"parallelism"/…); datum letter;
    # weld type per ГОСТ 2.312
    symbol: str | None = None
    # datum letters a geometric tolerance references, e.g. ["A", "B"]
    datum_refs: list[str] = Field(default_factory=list)
    # optional leader line end at the annotated feature
    leader: Point | None = None
    height: float = Field(default=3.5, gt=0)
    line_class: LineClass = "dim"
    width_class: WidthClass = "thin"


Entity = Annotated[
    Union[Segment, Arc, Circle, Polyline, TextEntity, DimensionEntity, HatchRegion, AnnotationEntity],
    Field(discriminator="type"),
]


class SourceInfo(BaseModel):
    generation_id: str | None = None
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    kind: Literal["scan", "photo", "blank", "spec", "import"] = "scan"


class SheetInfo(BaseModel):
    # ГОСТ 2.301 format name when detected/chosen (A4, A3, ...), None otherwise
    format: str | None = None
    width_mm: float | None = None
    height_mm: float | None = None
    frame: bool = False
    title_block: dict = Field(default_factory=dict)
    # Detected sheet-frame bounding box in source pixels [x, y, w, h] — kept
    # so the editor can recompute mm/px when the user confirms the format
    # (B6), since A-series aspect ratios are identical and pixels alone can't
    # tell A4 from A0. None when no frame was detected.
    frame_px: list[float] | None = None


class ValidationIssueIR(BaseModel):
    code: str
    severity: Literal["error", "warn", "info"] = "warn"
    entity_ids: list[str] = Field(default_factory=list)
    message_ru: str = ""
    # Assurance-pipeline level (Ф7.1): 1=схема/валидность IR, 2=геометрия,
    # 3=точные размеры/цепи, 4=ЕСКД-оформление, 5=технологичность,
    # 6=normcontrol (LLM), 7=VLM-критик. 0 = recognition-quality signal
    # (coverage/neural availability) — a different axis, not an engineering
    # correctness level.
    level: int = 0
    # Ф9: which standard this check enforces, e.g. "ГОСТ 2.303-68" — a plain
    # citation string, always present when the check has one, independent of
    # whether the corpus actually has that document ingested. When it does
    # (see app.ai.norm_citation.resolve_norm_citations), norm_clause_text
    # gets filled with the real stored clause instead of staying empty —
    # "cite always, resolve to stored data when available."
    norm_ref: str | None = None
    norm_clause_text: str | None = None
    # C2 machine-readable ЕСКД profile: a stable rule key (e.g.
    # "ESKD.2.303.line_weight") independent of the plain citation, and a
    # concrete fix path ("сделайте линию тонкой в свойствах"). Both come from
    # the versioned rule registry in app.ai.eskd_profile; None for checks not
    # in the profile (geometry/recognition-quality signals).
    rule_id: str | None = None
    fix_hint: str | None = None


class ValidationReportIR(BaseModel):
    issues: list[ValidationIssueIR] = Field(default_factory=list)
    # raster coverage of the recognized geometry vs the source ink
    coverage_recall: float | None = None
    coverage_precision: float | None = None
    # Export fidelity calculated from vector entities only. The legacy
    # coverage values remain readable for old stored revisions.
    vector_recall: float | None = None
    vector_precision: float | None = None
    raster_passthrough_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    dxf_reopens: bool | None = None
    # C2: which versioned ЕСКД ruleset produced these findings — a stored
    # report stays interpretable after the profile tightens.
    eskd_profile_version: str | None = None

    @property
    def blocking(self) -> list[ValidationIssueIR]:
        return [i for i in self.issues if i.severity == "error"]

    def by_level(self) -> dict[int, list[ValidationIssueIR]]:
        """Group issues by assurance-pipeline level (Ф7.1) for a level-by-
        level report — level 0 (recognition-quality signals) included."""
        out: dict[int, list[ValidationIssueIR]] = {}
        for issue in self.issues:
            out.setdefault(issue.level, []).append(issue)
        return out


class ReviewItem(BaseModel):
    entity_id: str
    reason: str
    resolved: bool = False


class CadEntityRelation(BaseModel):
    """Persisted semantic relation between stable CadIR entity identifiers."""

    id: str = Field(default_factory=_entity_id)
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
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    assurance: Assurance = "inferred"
    evidence: list[str] = Field(default_factory=list)


ConstraintKind = Literal[
    "coincident", "horizontal", "vertical", "parallel", "perpendicular",
    "tangent", "concentric", "equal", "distance", "angle", "radius", "diameter",
]


class SketchPointRef(BaseModel):
    """Stable sub-entity reference used by a geometric constraint."""

    entity_id: str
    point: Literal["p1", "p2", "center"]


class CadParameter(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value: float
    unit: Literal["mm", "deg", "unitless"] = "mm"
    expression: str | None = None


class GeometricConstraint(BaseModel):
    id: str = Field(default_factory=_entity_id)
    kind: ConstraintKind
    refs: list[SketchPointRef] = Field(default_factory=list, max_length=2)
    entity_ids: list[str] = Field(default_factory=list, max_length=2)
    value: float | None = None
    parameter: str | None = None
    tolerance: float = Field(default=1e-3, gt=0)
    enabled: bool = True
    # A1: a driven (reference) dimension measures the geometry instead of
    # driving it — excluded from the solver and always "satisfied", it just
    # reports the current value. Default is a driving dimension.
    driven: bool = False

    @model_validator(mode="after")
    def _has_targets(self) -> "GeometricConstraint":
        if not self.refs and not self.entity_ids:
            raise ValueError("constraint requires refs or entity_ids")
        if self.parameter and self.value is not None:
            raise ValueError("constraint may use either value or parameter, not both")
        return self


class SketchConfiguration(BaseModel):
    """A1: a named set of parameter values (like a SolidWorks configuration).
    Activating it writes ``values`` onto the matching parameters and re-solves,
    so one sketch can carry a family of sizes."""

    name: str = Field(min_length=1, max_length=80)
    values: dict[str, float] = Field(default_factory=dict)


class BlockDef(BaseModel):
    """A2: a reusable named block — a geometry snapshot normalized to its base
    point. Inserting stamps translated/rotated COPIES into the sheet (the
    entities stay ordinary IR entities, so every renderer/export works
    unchanged); the definition itself is document data, not drawn."""

    name: str = Field(min_length=1, max_length=80)
    base: Point
    entities: list[Entity] = Field(default_factory=list)


class CadIR(BaseModel):
    schema_version: int = SCHEMA_VERSION

    @model_validator(mode="after")
    def _migrate_v1(self) -> "CadIR":
        """v1 → v2: derive the assurance rung from origin. Deterministic and
        idempotent — stored v1 revisions upgrade transparently on load."""
        if self.schema_version < 2:
            for entity in self.entities:
                if entity.origin == "human":
                    entity.assurance = "human_approved"
                elif entity.origin == "spec":
                    # spec-born geometry already passed techdraw_validate
                    entity.assurance = "constraint_validated"
                else:
                    entity.assurance = "inferred"
            self.schema_version = SCHEMA_VERSION
        if self.scale is not None and self.scale_source is None and self.source.kind in ("blank", "spec"):
            self.scale_source = "sheet_format"
        if self.schema_version < 3:
            self.schema_version = SCHEMA_VERSION
        if self.schema_version < 4:
            if self.source.kind in ("scan", "photo"):
                self.digitization_status = "review_required"
            self.schema_version = SCHEMA_VERSION
        if self.schema_version < 5:
            self.schema_version = SCHEMA_VERSION
        return self
    units: Literal["mm"] = "mm"
    # mm per source pixel; None until frame detection / manual input
    scale: float | None = Field(default=None, gt=0)
    # A metric scale is engineering evidence, not an aspect-ratio guess.
    scale_source: Literal["manual", "calibration", "dpi", "sheet_format"] | None = None
    source: SourceInfo
    sheet: SheetInfo = Field(default_factory=SheetInfo)
    entities: list[Entity] = Field(default_factory=list)
    relations: list[CadEntityRelation] = Field(default_factory=list)
    validation: ValidationReportIR = Field(default_factory=ValidationReportIR)
    review: list[ReviewItem] = Field(default_factory=list)
    unresolved_regions: list[UnresolvedRegion] = Field(default_factory=list)
    digitization_status: Literal[
        "exact_candidate", "review_required", "refused"
    ] = "review_required"
    parameters: list[CadParameter] = Field(default_factory=list)
    constraints: list[GeometricConstraint] = Field(default_factory=list)
    configurations: list[SketchConfiguration] = Field(default_factory=list)
    blocks: list[BlockDef] = Field(default_factory=list)
    # which recognizer produced revision 0: neural | cv | mixed | manual
    recognizer_used: str | None = None

    @model_validator(mode="after")
    def _validate_relation_references(self) -> "CadIR":
        entity_ids = [entity.id for entity in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("CadIR entity ids must be unique")
        relation_ids = [relation.id for relation in self.relations]
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("CadIR relation ids must be unique")
        known = set(entity_ids)
        for relation in self.relations:
            refs = [relation.source_entity_id, *relation.target_entity_ids]
            missing = sorted({ref for ref in refs if ref not in known})
            if missing:
                raise ValueError(
                    f"relation {relation.id} references missing entities: {missing}"
                )
        return self

    def entity_by_id(self, entity_id: str) -> Entity | None:
        for entity in self.entities:
            if entity.id == entity_id:
                return entity
        return None

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entity in self.entities:
            out[entity.type] = out.get(entity.type, 0) + 1
        return out
