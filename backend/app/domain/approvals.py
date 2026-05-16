"""Pydantic schemas for Approval domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

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
    chain_root_id: uuid.UUID | None = None
    chain_order: int | None = None

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


# ── Chain schemas ─────────────────────────────────────────────────────────────

class ChainStep(BaseModel):
    assigned_to: str
    comment: str | None = None


class ApprovalChainCreate(BaseModel):
    action_type: ApprovalActionType
    entity_type: str
    entity_id: uuid.UUID
    requested_by: str = "sveta"
    context: dict | None = None
    expires_at: datetime | None = None
    steps: list[ChainStep] = Field(..., min_length=2, description="At least 2 approvers")

    @model_validator(mode="after")
    def check_steps(self) -> "ApprovalChainCreate":
        if len(self.steps) < 2:
            raise ValueError("Chain must have at least 2 steps")
        return self


class ApprovalChainOut(BaseModel):
    chain_root_id: uuid.UUID
    steps: list[ApprovalOut]
    total_steps: int
