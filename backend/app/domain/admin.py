"""Admin domain — Pydantic schemas for user management and audit."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    sub: str
    email: str
    name: str
    preferred_username: str
    role: str
    is_active: bool
    last_seen_at: datetime | None
    created_at: datetime
    department_id: uuid.UUID | None = None
    manager_sub: str | None = None
    title: str | None = None
    # Per-user section allowlist (None = base sections only for non-admins).
    section_access: list[str] | None = None

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    name: str
    email: str
    role: str = "viewer"
    preferred_username: str = ""
    password: str = ""


class UserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    name: str | None = None
    preferences: dict | None = None
    # Organization placement. Use model_fields_set in the endpoint to tell "absent"
    # from an explicit null (which clears the field).
    department_id: uuid.UUID | None = None
    manager_sub: str | None = None
    title: str | None = None
    # Per-user section allowlist. Absent = leave unchanged; a list = replace the
    # grant (unknown/non-assignable keys are dropped server-side). Admins bypass
    # section access, so setting it on an admin has no effect on what they see.
    section_access: list[str] | None = None


class DepartmentCreate(BaseModel):
    name: str
    code: str
    parent_id: uuid.UUID | None = None


class DepartmentUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    parent_id: uuid.UUID | None = None


class DepartmentOut(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    parent_id: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DepartmentListResponse(BaseModel):
    items: list[DepartmentOut]
    total: int


class IntegrationAuthentikOut(BaseModel):
    auth_enabled: bool
    external_url: str
    admin_url: str
    token_set: bool
    token_hint: str  # masked — never the full token


class IntegrationAuthentikUpdate(BaseModel):
    # Provide api_token to set it; empty string clears. Omit to leave unchanged.
    api_token: str | None = None
    external_url: str | None = None


class IntegrationTestResult(BaseModel):
    ok: bool
    detail: str


class SetPasswordRequest(BaseModel):
    password: str


# ── Mail server (Mailcow) integration ───────────────────────────────────────


class IntegrationMailServerOut(BaseModel):
    configured: bool
    api_url: str | None = None
    api_key_set: bool
    api_key_hint: str  # masked — never the full key
    mail_domain: str | None = None
    webmail_url: str | None = None
    imap_host: str | None = None
    imap_port: int
    smtp_host: str | None = None
    smtp_port: int
    default_quota_mb: int = 1024


class IntegrationMailServerUpdate(BaseModel):
    # Provide api_key to set it; empty string clears. Omit to leave unchanged.
    api_url: str | None = None
    api_key: str | None = None
    mail_domain: str | None = None
    webmail_url: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    default_quota_mb: int | None = Field(default=None, ge=0, le=1024 * 1024)
    # Save-and-verify in one click: run the connection probe after storing and
    # return its verdict, instead of making the admin press two buttons.
    verify: bool = False


class IntegrationMailServerSaved(IntegrationMailServerOut):
    """PUT result: the stored config plus the optional verification verdict."""

    verified: bool | None = None
    verify_detail: str | None = None


# ── OAuth apps (Gmail / Microsoft 365 mailbox auth) ─────────────────────────
# One Client ID/Secret per provider, registered once by an admin; individual
# mailboxes then each run their own consent flow against it (app/api/oauth.py).


class OAuthAppOut(BaseModel):
    provider: str
    client_id: str | None = None
    client_secret_set: bool
    client_secret_hint: str  # masked — never the full secret
    redirect_uri: str | None = None
    configured: bool


class OAuthAppUpdate(BaseModel):
    # Provide client_secret to set it; empty string clears. Omit to leave unchanged.
    client_id: str | None = None
    client_secret: str | None = None
    redirect_uri: str | None = None


class UserMailboxOut(BaseModel):
    address: str | None = None
    is_active: bool | None = None
    webmail_url: str | None = None
    last_sync_at: datetime | None = None
    sync_error: str | None = None
    # Consent switch for AI triage (mailbox_configs.sweep_enabled). Off by
    # default for personal mailboxes — see app/tasks/email_triage.py.
    sweep_enabled: bool | None = None
    quota_mb: int | None = None


class UserMailboxCreate(BaseModel):
    local_part: str | None = None  # omit to auto-suggest from the user's name/username
    quota_mb: int | None = Field(default=None, ge=0, le=1024 * 1024)


class UserMailboxSweepUpdate(BaseModel):
    """Owner-controlled consent: let the agent read this mailbox, or stop it."""

    sweep_enabled: bool


class UserMailboxRevoke(BaseModel):
    """Revoke a personal mailbox.

    ``delete_on_server=False`` (default) only deactivates it in Mailcow and in our
    config — the mail is preserved. ``True`` destroys the mailbox with all of its
    correspondence and therefore requires ``confirm_address`` to match exactly.
    """

    delete_on_server: bool = False
    confirm_address: str | None = None


class UserMailboxProvisionedOut(BaseModel):
    address: str
    generated_password: str  # shown once; never retrievable again


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
