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

SCHEMA_VERSION = 3

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


Entity = Annotated[
    Union[Segment, Arc, Circle, Polyline, TextEntity, DimensionEntity, HatchRegion],
    Field(discriminator="type"),
]


class SourceInfo(BaseModel):
    generation_id: str | None = None
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    kind: Literal["scan", "photo", "blank", "spec"] = "scan"


class SheetInfo(BaseModel):
    # ГОСТ 2.301 format name when detected/chosen (A4, A3, ...), None otherwise
    format: str | None = None
    width_mm: float | None = None
    height_mm: float | None = None
    frame: bool = False
    title_block: dict = Field(default_factory=dict)


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


class ValidationReportIR(BaseModel):
    issues: list[ValidationIssueIR] = Field(default_factory=list)
    # raster coverage of the recognized geometry vs the source ink
    coverage_recall: float | None = None
    coverage_precision: float | None = None

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

    @model_validator(mode="after")
    def _has_targets(self) -> "GeometricConstraint":
        if not self.refs and not self.entity_ids:
            raise ValueError("constraint requires refs or entity_ids")
        if self.parameter and self.value is not None:
            raise ValueError("constraint may use either value or parameter, not both")
        return self


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
        return self
    units: Literal["mm"] = "mm"
    # mm per source pixel; None until frame detection / manual input
    scale: float | None = Field(default=None, gt=0)
    # A metric scale is engineering evidence, not an aspect-ratio guess.
    scale_source: Literal["manual", "calibration", "dpi", "sheet_format"] | None = None
    source: SourceInfo
    sheet: SheetInfo = Field(default_factory=SheetInfo)
    entities: list[Entity] = Field(default_factory=list)
    validation: ValidationReportIR = Field(default_factory=ValidationReportIR)
    review: list[ReviewItem] = Field(default_factory=list)
    parameters: list[CadParameter] = Field(default_factory=list)
    constraints: list[GeometricConstraint] = Field(default_factory=list)
    # which recognizer produced revision 0: neural | cv | mixed | manual
    recognizer_used: str | None = None

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
