"""Pydantic schemas for Email domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class EmailMessageOut(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID | None
    message_id_header: str | None
    mailbox: str
    from_address: str
    to_addresses: list[str] | None
    cc_addresses: list[str] | None
    subject: str | None
    body_text: str | None
    sent_at: datetime | None
    received_at: datetime | None
    has_attachments: bool
    attachment_count: int
    attachments_meta: list | None
    is_inbound: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class EmailFetchRequest(BaseModel):
    mailbox: str | None = None  # None = all configured mailboxes


class EmailFetchResponse(BaseModel):
    fetched_count: int
    new_messages: list[EmailMessageOut] = []
    errors: list[str] = []
    task_id: str | None = None


# ── Thread ─────────────────────────────────────────────────────────────────


class EmailThreadOut(BaseModel):
    id: uuid.UUID
    subject: str
    mailbox: str
    party_id: uuid.UUID | None
    message_count: int
    last_message_at: datetime | None
    messages: list[EmailMessageOut] = []
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Draft ──────────────────────────────────────────────────────────────────


class EmailDraftCreate(BaseModel):
    to_addresses: list[str]
    cc_addresses: list[str] = []
    subject: str
    body_html: str
    body_text: str | None = None
    thread_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None
    context: dict | None = None  # invoice_id, document_id, etc.


class EmailDraftOut(BaseModel):
    id: uuid.UUID
    to_addresses: list[str]
    cc_addresses: list[str] | None
    subject: str
    body_html: str | None
    body_text: str | None
    thread_id: uuid.UUID | None
    status: str  # draft, risk_checked, approved, sent
    risk_flags: list[dict] = []
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Style Match ────────────────────────────────────────────────────────────


class StyleAnalyzeRequest(BaseModel):
    supplier_id: uuid.UUID | None = None
    email_address: str | None = None
    sample_count: int = 5


class StyleAnalyzeResponse(BaseModel):
    tone: str  # formal, friendly, neutral
    language: str  # ru, en
    greeting_style: str | None = None
    closing_style: str | None = None
    avg_length: int = 0
    recommendations: list[str] = []
    sample_count: int = 0


# ── Risk Check ─────────────────────────────────────────────────────────────


class RiskFlag(BaseModel):
    code: str
    severity: str  # warning, error
    message: str
    can_override: bool = True


class RiskCheckResponse(BaseModel):
    draft_id: uuid.UUID
    is_safe: bool
    flags: list[RiskFlag] = []


# ── Suggest Template ───────────────────────────────────────────────────────


class TemplateSuggestRequest(BaseModel):
    context_type: str  # payment_reminder, price_inquiry, order_confirmation, custom
    supplier_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None
    language: str = "ru"


class EmailTemplate(BaseModel):
    name: str
    subject: str
    body_html: str
    body_text: str
    variables: list[str] = []


class TemplateSuggestResponse(BaseModel):
    templates: list[EmailTemplate]
    recommended: str | None = None


# ── Email Search ───────────────────────────────────────────────────────────


class EmailSearchRequest(BaseModel):
    query: str | None = None
    supplier_id: uuid.UUID | None = None
    email_address: str | None = None
    mailbox: str | None = None
    limit: int = 20


class EmailSearchResponse(BaseModel):
    results: list[EmailMessageOut]
    total: int
