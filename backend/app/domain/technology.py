"""Pydantic schemas for manufacturing technology skills."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ManufacturingResourceCreate(BaseModel):
    resource_type: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=500)
    code: str | None = None
    model: str | None = None
    standard: str | None = None
    capabilities: dict | None = None
    location: str | None = None
    status: str = "active"
    notes: str | None = None
    metadata_: dict | None = Field(None, alias="metadata")


class ManufacturingResourceOut(ManufacturingResourceCreate):
    id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class ProcessPlanCreate(BaseModel):
    document_id: uuid.UUID | None = None
    bom_id: uuid.UUID | None = None
    product_name: str = Field(..., min_length=1, max_length=500)
    product_code: str | None = None
    version: str = "1.0"
    status: str = "draft"
    standard_system: str = "ЕСТД"
    route_summary: str | None = None
    material: str | None = None
    blank_type: str | None = None
    quality_requirements: str | None = None
    created_by: str = "sveta"
    metadata_: dict | None = Field(None, alias="metadata")


class ProcessPlanDraftFromDocumentRequest(BaseModel):
    document_id: uuid.UUID
    product_name: str | None = Field(None, max_length=500)
    product_code: str | None = None
    created_by: str = "sveta"
    rebuild_existing: bool = False


class ProcessPlanApproveRequest(BaseModel):
    approved_by: str = Field(..., min_length=1, max_length=100)
    comment: str | None = None


class ProcessPlanOut(ProcessPlanCreate):
    id: uuid.UUID
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class OperationCreate(BaseModel):
    sequence_no: int = Field(..., ge=1)
    operation_code: str | None = None
    name: str = Field(..., min_length=1, max_length=500)
    operation_type: str | None = None
    machine_resource_id: uuid.UUID | None = None
    tool_resource_id: uuid.UUID | None = None
    fixture_resource_id: uuid.UUID | None = None
    setup_description: str | None = None
    transition_text: str | None = None
    cutting_parameters: dict | None = None
    control_requirements: str | None = None
    safety_requirements: str | None = None
    setup_minutes: float | None = Field(None, ge=0)
    machine_minutes: float | None = Field(None, ge=0)
    labor_minutes: float | None = Field(None, ge=0)
    metadata_: dict | None = Field(None, alias="metadata")


class OperationOut(OperationCreate):
    id: uuid.UUID
    process_plan_id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class NormEstimateCreate(BaseModel):
    operation_id: uuid.UUID | None = None
    setup_minutes: float | None = Field(None, ge=0)
    machine_minutes: float | None = Field(None, ge=0)
    labor_minutes: float | None = Field(None, ge=0)
    batch_size: float | None = Field(None, gt=0)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    method: str = "manual"
    assumptions: list[str] | None = None
    created_by: str = "sveta"
    metadata_: dict | None = Field(None, alias="metadata")


class NormEstimateApproveRequest(BaseModel):
    approved_by: str = Field(..., min_length=1, max_length=100)
    comment: str | None = None


class ProcessPlanEstimateNormsRequest(BaseModel):
    batch_size: float = Field(1.0, gt=0)
    overwrite_existing: bool = False
    created_by: str = "sveta"


class NormEstimateOut(NormEstimateCreate):
    id: uuid.UUID
    process_plan_id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class OperationTemplateCreate(BaseModel):
    operation_type: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=500)
    standard_system: str = "ЕСТД"
    default_operation_code: str | None = None
    required_resource_types: list[str] | None = None
    default_transition_text: str | None = None
    default_control_requirements: str | None = None
    default_safety_requirements: str | None = None
    parameters_schema: dict | None = None
    is_active: bool = True
    metadata_: dict | None = Field(None, alias="metadata")


class OperationTemplateOut(OperationTemplateCreate):
    id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class TechnologyCheckOut(BaseModel):
    id: uuid.UUID
    process_plan_id: uuid.UUID
    operation_id: uuid.UUID | None = None
    check_code: str
    severity: str
    status: str
    message: str
    recommendation: str | None = None
    evidence: dict | None = None
    created_by: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TechnologyCheckResponse(BaseModel):
    process_plan_id: uuid.UUID
    checks: list[TechnologyCheckOut]
    total: int
    critical: int
    warnings: int


class ProcessPlanEstimateNormsResponse(BaseModel):
    process_plan_id: uuid.UUID
    estimates: list[NormEstimateOut]
    created: int
    skipped_existing: int


class TechnologyCorrectionCreate(BaseModel):
    entity_type: str = Field(..., min_length=1, max_length=80)
    entity_id: uuid.UUID
    field_name: str = Field(..., min_length=1, max_length=120)
    old_value: str | None = None
    new_value: str | None = None
    correction_type: str = "manual_edit"
    corrected_by: str = Field(..., min_length=1, max_length=100)
    reason: str | None = None
    source_document_id: uuid.UUID | None = None
    process_plan_id: uuid.UUID | None = None
    operation_id: uuid.UUID | None = None
    metadata_: dict | None = Field(None, alias="metadata")


class TechnologyCorrectionOut(TechnologyCorrectionCreate):
    id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class LearningSuggestionOut(BaseModel):
    suggestion_type: str
    entity_type: str
    field_name: str
    old_value: str | None = None
    new_value: str | None = None
    occurrences: int
    confidence: float = Field(..., ge=0.0, le=1.0)
    recommendation: str


class LearningSuggestionResponse(BaseModel):
    suggestions: list[LearningSuggestionOut]
    total: int


class LearningRuleCreate(BaseModel):
    rule_type: str = "normalization_rule"
    entity_type: str = Field(..., min_length=1, max_length=80)
    field_name: str = Field(..., min_length=1, max_length=120)
    match_old_value: str | None = None
    replacement_value: str | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    occurrences: int = Field(0, ge=0)
    status: str = "proposed"
    suggested_by: str = "system"
    metadata_: dict | None = Field(None, alias="metadata")


class LearningRuleActivateRequest(BaseModel):
    activated_by: str = Field(..., min_length=1, max_length=100)
    comment: str | None = None


class LearningRuleOut(LearningRuleCreate):
    id: uuid.UUID
    activated_by: str | None = None
    activated_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class LearningRuleListResponse(BaseModel):
    items: list[LearningRuleOut]
    total: int


class ProcessPlanDetail(ProcessPlanOut):
    operations: list[OperationOut] = []
    norm_estimates: list[NormEstimateOut] = []


class ResourceListResponse(BaseModel):
    items: list[ManufacturingResourceOut]
    total: int


class ProcessPlanListResponse(BaseModel):
    items: list[ProcessPlanOut]
    total: int


class ProcessPlanDraftFromDocumentResponse(BaseModel):
    process_plan: ProcessPlanDetail
    resources_created: int
    operations_created: int
    source_mentions: dict[str, list[str]]


class OperationTemplateListResponse(BaseModel):
    items: list[OperationTemplateOut]
    total: int
