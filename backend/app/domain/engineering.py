"""Public contracts for the canonical engineering-project API."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import AliasChoices, BaseModel, Field


class EngineeringProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    code: str | None = Field(default=None, max_length=100)
    project_id: uuid.UUID | None = None
    description: str | None = None
    metadata_: dict = Field(
        default_factory=dict,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class EngineeringProjectOut(EngineeringProjectCreate):
    id: uuid.UUID
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class EngineeringRevisionCreate(BaseModel):
    base_revision: int | None = Field(default=None, ge=0)
    payload: dict = Field(default_factory=dict)
    validation: dict = Field(default_factory=dict)
    origin: str = Field(default="manual", max_length=30)
    change_summary: str | None = None
    created_by: str | None = Field(default=None, max_length=255)


class EngineeringRevisionOut(BaseModel):
    id: uuid.UUID
    engineering_project_id: uuid.UUID
    revision: int
    base_revision: int | None
    status: str
    origin: str
    change_summary: str | None
    payload: dict
    validation: dict
    created_by: str | None
    approved_by: str | None
    approved_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EngineeringProjectionCreate(BaseModel):
    projection_type: str = Field(min_length=1, max_length=40)
    entity_type: str = Field(min_length=1, max_length=80)
    entity_id: uuid.UUID
    metadata_: dict = Field(
        default_factory=dict,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class EngineeringProjectionOut(EngineeringProjectionCreate):
    id: uuid.UUID
    engineering_revision_id: uuid.UUID
    state: str
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class EngineeringApprovalRequest(BaseModel):
    approved_by: str = Field(min_length=1, max_length=255)


class EngineeringMaterialCreate(BaseModel):
    designation: str = Field(min_length=1, max_length=160)
    standard: str | None = Field(default=None, max_length=160)
    description: str | None = None
    density_kg_m3: float | None = Field(default=None, gt=0)
    elastic_modulus_mpa: float | None = Field(default=None, gt=0)
    yield_strength_mpa: float | None = Field(default=None, gt=0)
    tensile_strength_mpa: float | None = Field(default=None, gt=0)
    thermal_expansion_1_k: float | None = Field(default=None, gt=0)
    metadata_: dict = Field(default_factory=dict, validation_alias=AliasChoices("metadata_", "metadata"), serialization_alias="metadata")


class EngineeringMaterialOut(EngineeringMaterialCreate):
    id: uuid.UUID
    created_at: datetime
    model_config = {"from_attributes": True, "populate_by_name": True}


class EngineeringMaterialAssignmentCreate(BaseModel):
    material_id: uuid.UUID
    object_key: str = Field(min_length=1, max_length=160)
    source: str = Field(default="manual", max_length=30)
    confidence: float = Field(default=1.0, ge=0, le=1)
    metadata_: dict = Field(default_factory=dict, validation_alias=AliasChoices("metadata_", "metadata"), serialization_alias="metadata")


class EngineeringMaterialAssignmentOut(EngineeringMaterialAssignmentCreate):
    id: uuid.UUID
    engineering_revision_id: uuid.UUID
    created_at: datetime
    material: EngineeringMaterialOut
    model_config = {"from_attributes": True, "populate_by_name": True}


class EngineeringAssemblyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    designation: str | None = Field(default=None, max_length=160)
    metadata_: dict = Field(default_factory=dict, validation_alias=AliasChoices("metadata_", "metadata"), serialization_alias="metadata")


class EngineeringAssemblyOut(EngineeringAssemblyCreate):
    id: uuid.UUID
    engineering_revision_id: uuid.UUID
    created_at: datetime
    model_config = {"from_attributes": True, "populate_by_name": True}


class EngineeringAssemblyComponentCreate(BaseModel):
    component_revision_id: uuid.UUID | None = None
    instance_key: str = Field(min_length=1, max_length=160)
    designation: str = Field(min_length=1, max_length=300)
    quantity: int = Field(default=1, ge=1)
    sort_order: int = 0
    transform: dict = Field(default_factory=dict)
    bounds: dict | None = None
    suppressed: bool = False
    metadata_: dict = Field(default_factory=dict, validation_alias=AliasChoices("metadata_", "metadata"), serialization_alias="metadata")


class EngineeringAssemblyComponentOut(EngineeringAssemblyComponentCreate):
    id: uuid.UUID
    engineering_assembly_id: uuid.UUID
    created_at: datetime
    model_config = {"from_attributes": True, "populate_by_name": True}


class EngineeringAssemblyMateCreate(BaseModel):
    mate_type: str = Field(min_length=1, max_length=40)
    first_instance_key: str = Field(min_length=1, max_length=160)
    second_instance_key: str = Field(min_length=1, max_length=160)
    parameters: dict = Field(default_factory=dict)


class EngineeringAssemblyMateOut(EngineeringAssemblyMateCreate):
    id: uuid.UUID
    engineering_assembly_id: uuid.UUID
    status: str
    created_at: datetime
    model_config = {"from_attributes": True}


class EngineeringAssemblyValidation(BaseModel):
    assembly_id: uuid.UUID
    collisions: list[tuple[str, str]] = Field(default_factory=list)
    invalid_mates: list[str] = Field(default_factory=list)
    # E5: exact B-Rep results — pairs with the actual intersection volume;
    # which instances were kernel-checked; a loud note when the kernel path
    # degraded to AABB (never a silent fallback).
    exact_collisions: list[dict] = Field(default_factory=list)
    exact_checked: list[str] = Field(default_factory=list)
    degraded: str | None = None


class EngineeringValidationRunOut(BaseModel):
    id: uuid.UUID
    engineering_revision_id: uuid.UUID
    status: str
    summary: dict
    findings: list[dict]
    initiated_by: str | None
    created_at: datetime
    model_config = {"from_attributes": True}


class EngineeringAnalysisCaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    analysis_type: str = Field(default="axial_stress", max_length=50)
    material_id: uuid.UUID | None = None
    inputs: dict = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)


class EngineeringAnalysisCaseOut(EngineeringAnalysisCaseCreate):
    id: uuid.UUID
    engineering_revision_id: uuid.UUID
    status: str
    results: dict
    solver: str
    executed_at: datetime | None
    created_at: datetime
    model_config = {"from_attributes": True}


class EngineeringProjectDetail(EngineeringProjectOut):
    revisions: list[EngineeringRevisionOut] = Field(default_factory=list)


class ChangeRequestCreate(BaseModel):
    """E3: a change request must say WHAT (title), WHY (reason) and against
    WHICH revision; reviewers are the people whose signatures gate approval."""

    title: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=3, max_length=4000)
    affected_revision_id: uuid.UUID
    reviewers: list[str] = Field(default_factory=list, max_length=20)
    supersedes_id: uuid.UUID | None = None
    created_by: str | None = Field(default=None, max_length=255)


class ChangeRequestSign(BaseModel):
    reviewer: str = Field(min_length=1, max_length=255)
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=2000)


class ChangeRequestOut(BaseModel):
    id: uuid.UUID
    engineering_project_id: uuid.UUID
    number: int
    title: str
    reason: str
    status: str
    affected_revision_id: uuid.UUID
    impact: dict
    reviewers: list
    signatures: list
    supersedes_id: uuid.UUID | None
    applied_revision_id: uuid.UUID | None
    created_by: str | None
    decided_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EngineeringAnalysisRunOut(BaseModel):
    """F2: an immutable execution record — inputs, material card and solver
    version frozen at run time."""

    id: uuid.UUID
    analysis_case_id: uuid.UUID
    run_number: int
    status: str
    inputs_snapshot: dict
    material_snapshot: dict | None
    solver_name: str
    solver_version: str
    results: dict
    assumptions: list
    error: str | None
    executed_by: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
