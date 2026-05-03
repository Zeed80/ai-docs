"""Schemas for official AiAgent Gateway control callbacks."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AiAgentApprovalRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=100)
    iteration: int = Field(0, ge=0)
    tool_name: str = Field(..., min_length=1, max_length=100)
    tool_args: dict | None = None
    assigned_to: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    reason: str | None = None


class AiAgentApprovalTicket(BaseModel):
    approval_id: uuid.UUID
    agent_action_id: uuid.UUID
    status: str
    tool_name: str
    created_at: datetime


class AiAgentResumeStatus(BaseModel):
    approval_id: uuid.UUID
    status: str
    approved: bool
    rejected: bool
    tool_name: str | None = None
    tool_args: dict | None = None
    decision_comment: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
