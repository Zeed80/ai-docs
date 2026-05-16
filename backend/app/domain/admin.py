"""Admin domain — Pydantic schemas for user management and audit."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr


class UserOut(BaseModel):
    sub: str
    email: str
    name: str
    preferred_username: str
    role: str
    is_active: bool
    last_seen_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    name: str
    email: str
    role: str = "viewer"
    preferred_username: str = ""


class UserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    preferences: dict | None = None


class UserListResponse(BaseModel):
    items: list[UserOut]
    total: int


class ApiKeyCreate(BaseModel):
    name: str
    scopes: list[str]
    expires_at: datetime | None = None


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    user_sub: str
    scopes: list[str]
    is_active: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyCreatedOut(ApiKeyOut):
    raw_key: str  # shown ONCE, never stored


class ApiKeyListResponse(BaseModel):
    items: list[ApiKeyOut]
    total: int


class AuditLogOut(BaseModel):
    id: uuid.UUID
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    user_id: str | None
    ip_address: str | None
    details: dict | None
    timestamp: datetime

    model_config = {"from_attributes": True}


class AuditLogListResponse(BaseModel):
    items: list[AuditLogOut]
    total: int


class PermissionMatrixOut(BaseModel):
    matrix: dict[str, list[str]]


class SystemStatusOut(BaseModel):
    db: str
    redis: str
    celery: str
    ai_providers: dict[str, str]
    active_users_count: int
    pending_approvals_count: int
