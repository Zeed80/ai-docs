"""CAD IR — engineering intermediate representation for the studio pipeline.

The IR is the single source of truth between recognition (neural/VLM/CV),
validation, the review/editor UI and every render target (PNG/SVG/DXF/DWG,
later STEP). Recognition backends only *fill* the IR with per-entity
confidence; renders are deterministic functions of it; edits create new
revisions (``CadIrRevision``) and re-render without any LLM involvement.
"""

from app.ai.cad_ir.schema import (
    Arc,
    CadIR,
    Circle,
    CadParameter,
    DimensionEntity,
    Entity,
    GeometricConstraint,
    HatchRegion,
    Polyline,
    ReviewItem,
    Segment,
    SheetInfo,
    SourceInfo,
    TextEntity,
    ValidationIssueIR,
    ValidationReportIR,
)

__all__ = [
    "Arc",
    "CadIR",
    "Circle",
    "CadParameter",
    "DimensionEntity",
    "Entity",
    "GeometricConstraint",
    "HatchRegion",
    "Polyline",
    "ReviewItem",
    "Segment",
    "SheetInfo",
    "SourceInfo",
    "TextEntity",
    "ValidationIssueIR",
    "ValidationReportIR",
]
