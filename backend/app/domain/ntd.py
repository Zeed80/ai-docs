"""Schemas for NTD storage and norm-control checks."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


NTDControlMode = Literal["manual", "auto"]
NTDFindingDecision = Literal["accept", "reject", "mark_not_applicable", "create_correction_task"]


class NTDControlSettingsOut(BaseModel):
    mode: NTDControlMode = "manual"
    updated_by: str | None = None
    updated_at: datetime | None = None


class NTDControlSettingsUpdate(BaseModel):
    mode: NTDControlMode
    updated_by: str = Field("system", min_length=1, max_length=100)


class NormativeDocumentCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=500)
    document_type: str = Field("ГОСТ", min_length=1, max_length=50)
    version: str = Field("current", min_length=1, max_length=100)
    status: str = Field("active", pattern="^(draft|active|obsolete)$")
    scope: str | None = None
    source_document_id: uuid.UUID | None = None
    metadata_: dict | None = Field(None, alias="metadata")


class NormativeDocumentOut(BaseModel):
    id: uuid.UUID
    code: str
    title: str
    document_type: str
    status: str
    current_version_id: uuid.UUID | None = None
    scope: str | None = None
    source_document_id: uuid.UUID | None = None
    metadata_: dict | None = Field(None, alias="metadata")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class NormativeClauseCreate(BaseModel):
    normative_document_id: uuid.UUID
    version_id: uuid.UUID | None = None
    clause_number: str = Field(..., min_length=1, max_length=100)
    title: str | None = None
    text: str = Field(..., min_length=1)
    parent_clause_id: uuid.UUID | None = None
    metadata_: dict | None = Field(None, alias="metadata")


class NormativeClauseOut(BaseModel):
    id: uuid.UUID
    normative_document_id: uuid.UUID
    version_id: uuid.UUID | None = None
    clause_number: str
    title: str | None = None
    text: str
    parent_clause_id: uuid.UUID | None = None
    metadata_: dict | None = Field(None, alias="metadata")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class NormativeRequirementCreate(BaseModel):
    normative_document_id: uuid.UUID
    clause_id: uuid.UUID | None = None
    requirement_code: str = Field(..., min_length=1, max_length=120)
    requirement_type: str = Field("generic", min_length=1, max_length=80)
    applies_to: list[str] = Field(default_factory=list)
    text: str = Field(..., min_length=1)
    required_keywords: list[str] = Field(default_factory=list)
    severity: str = Field("warning", pattern="^(info|warning|error|critical)$")
    is_active: bool = True
    metadata_: dict | None = Field(None, alias="metadata")


class NormativeRequirementOut(BaseModel):
    id: uuid.UUID
    normative_document_id: uuid.UUID
    clause_id: uuid.UUID | None = None
    requirement_code: str
    requirement_type: str
    applies_to: list | None = None
    text: str
    required_keywords: list | None = None
    severity: str
    is_active: bool
    metadata_: dict | None = Field(None, alias="metadata")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class NTDRequirementSearchResponse(BaseModel):
    query: str
    requirements: list[NormativeRequirementOut]
    total: int


class NTDDocumentIndexRequest(BaseModel):
    source_document_id: uuid.UUID | None = None
    requirement_type: str = Field("generic", min_length=1, max_length=80)
    replace_existing: bool = False
    actor: str = Field("system", min_length=1, max_length=100)


class NTDDocumentCreateFromSourceRequest(BaseModel):
    source_document_id: uuid.UUID
    requirement_type: str = Field("generic", min_length=1, max_length=80)
    code: str | None = None
    title: str | None = None
    document_type: str | None = None
    version: str | None = None
    status: str = Field("active", pattern="^(draft|active|obsolete)$")
    index_immediately: bool = True
    actor: str = Field("system", min_length=1, max_length=100)


class NTDDocumentIndexResponse(BaseModel):
    normative_document_id: uuid.UUID
    source_document_id: uuid.UUID | None = None
    clauses_created: int
    requirements_created: int
    text_chars: int


class NTDDocumentCreateFromSourceResponse(BaseModel):
    normative_document: NormativeDocumentOut
    index_result: NTDDocumentIndexResponse | None = None


class NTDCheckRunRequest(BaseModel):
    document_id: uuid.UUID
    normative_document_ids: list[uuid.UUID] | None = None
    triggered_by: str = Field("manual", pattern="^(manual|auto)$")
    actor: str = Field("system", min_length=1, max_length=100)
    semantic_ai: bool = False
    semantic_max_requirements: int = Field(8, ge=1, le=30)


class NTDCheckRunOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    status: str
    mode: str
    triggered_by: str
    summary: str | None = None
    findings_total: int
    findings_open: int
    metadata_: dict | None = Field(None, alias="metadata")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class NTDCheckAvailabilityResponse(BaseModel):
    document_id: uuid.UUID
    can_check: bool
    reasons: list[str] = []
    active_requirements: int = 0
    has_text: bool = False
    mode: NTDControlMode = "manual"


class NTDFindingOut(BaseModel):
    id: uuid.UUID
    check_id: uuid.UUID
    document_id: uuid.UUID
    normative_document_id: uuid.UUID | None = None
    clause_id: uuid.UUID | None = None
    requirement_id: uuid.UUID | None = None
    severity: str
    status: str
    finding_code: str
    message: str
    evidence_text: str | None = None
    recommendation: str | None = None
    confidence: float
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_comment: str | None = None
    metadata_: dict | None = Field(None, alias="metadata")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class NTDCheckRunDetail(BaseModel):
    check: NTDCheckRunOut
    findings: list[NTDFindingOut]


class NTDFindingDecisionRequest(BaseModel):
    action: NTDFindingDecision
    decided_by: str = Field("system", min_length=1, max_length=100)
    comment: str | None = None
