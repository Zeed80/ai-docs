"""Pydantic schemas for Normalization domain — skill contracts."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models import NormRuleStatus


class NormRuleOut(BaseModel):
    id: uuid.UUID
    field_name: str
    pattern: str
    replacement: str
    is_regex: bool
    status: NormRuleStatus
    source_corrections: int
    suggested_by: str
    activated_by: str | None
    activated_at: datetime | None
    apply_count: int
    last_applied_at: datetime | None
    description: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NormRuleListResponse(BaseModel):
    items: list[NormRuleOut]
    total: int


class NormRuleCreate(BaseModel):
    field_name: str
    pattern: str
    replacement: str
    is_regex: bool = False
    description: str | None = None


class NormRuleSuggestRequest(BaseModel):
    """Trigger suggestion engine for a specific document or field."""

    document_id: uuid.UUID | None = None
    field_name: str | None = None
    min_corrections: int = Field(3, ge=1, description="Minimum repeated corrections to suggest a rule")


class NormRuleSuggestResponse(BaseModel):
    suggested_rules: list[NormRuleOut]
    total_corrections_analyzed: int


class NormApplyRequest(BaseModel):
    document_id: uuid.UUID


class NormApplyResult(BaseModel):
    document_id: uuid.UUID
    rules_applied: int
    fields_modified: list[dict]


class NormRuleActivateRequest(BaseModel):
    activated_by: str = "user"
