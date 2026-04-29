"""Pydantic schemas for Approval domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models import ApprovalActionType, ApprovalStatus


class ApprovalCreate(BaseModel):
    action_type: ApprovalActionType
    entity_type: str
    entity_id: uuid.UUID
    requested_by: str = "sveta"
    assigned_to: str | None = None
    context: dict | None = None
    expires_at: datetime | None = None


class ApprovalOut(BaseModel):
    id: uuid.UUID
    action_type: ApprovalActionType
    entity_type: str
    entity_id: uuid.UUID
    status: ApprovalStatus
    requested_by: str | None
    assigned_to: str | None
    context: dict | None
    decision_comment: str | None
    decided_at: datetime | None
    decided_by: str | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ApprovalDecision(BaseModel):
    status: ApprovalStatus = Field(..., description="approved or rejected")
    comment: str | None = None
    decided_by: str = "user"


class ApprovalListParams(BaseModel):
    status: ApprovalStatus | None = ApprovalStatus.pending
    action_type: ApprovalActionType | None = None
    offset: int = 0
    limit: int = Field(50, le=200)


class ApprovalListResponse(BaseModel):
    items: list[ApprovalOut]
    total: int
