"""Pydantic schemas for Email domain."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EmailAttachmentOut(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str | None = None
    size: int | None = None
    is_inline: bool = False
    content_id: str | None = None
    document_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class TriageResultOut(BaseModel):
    """Ф6.4 — the "что сделала Света" panel, as data.

    ``performed`` is what actually happened; ``proposed`` is what she suggests
    and did not do. Keeping them apart is what stops the panel from claiming
    work it never did.
    """

    category: str
    category_label: str
    confidence: float | None = None
    summary: str | None = None
    entities: dict = {}
    performed: list = []
    proposed: list = []
    model_name: str | None = None
    corrected_category: str | None = None
    status: str = "done"


class DerivedInvoiceOut(BaseModel):
    """What the agent made out of this letter (Ф6.3).

    The link existed only one way — Document.source_email_id pointed at the
    message — so a person reading the thread had no idea that an invoice had
    been created from it, and no way to jump to it.
    """

    invoice_id: uuid.UUID
    document_id: uuid.UUID
    invoice_number: str | None = None
    total_amount: float | None = None
    currency: str | None = None
    status: str
    supplier_name: str | None = None
    supplier_matched_by: str | None = None


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
    body_text_derived: bool = False
    body_html: str | None = None
    body_html_sanitized: str | None = None
    sent_at: datetime | None
    received_at: datetime | None
    has_attachments: bool
    attachment_count: int
    attachments_meta: list | None
    attachments: list[EmailAttachmentOut] = []
    is_inbound: bool
    is_read: bool = False
    is_starred: bool = False
    folder: str = "inbox"
    snippet: str | None = None
    references: str | None = None
    reply_to: str | None = None
    headers_meta: dict | None = None
    # Filled by the thread endpoint, not the ORM.
    # Ф1.4 — можно ли показать удалённые картинки этого письма сразу: либо
    # пользователь включил это для себя, либо доверяет этому отправителю.
    images_trusted: bool = False
    derived_invoices: list[DerivedInvoiceOut] = []
    triage: "TriageResultOut | None" = None
    # Какое правило фильтрации сработало на этом письме. Журнал пишется с
    # самого начала, но в интерфейсе не показывался: человек видел метку и не
    # знал, поставил её агент, правило или коллега.
    applied_rules: list[dict] = []
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("attachments", mode="after")
    @classmethod
    def _hide_inline_parts(cls, value: list[EmailAttachmentOut]) -> list[EmailAttachmentOut]:
        """Inline parts (cid: logos, embedded screenshots) are rendered inside
        the body, not attached by the sender — listing them buries the real
        attachments under signature images. They stay reachable through the
        cid endpoint."""
        return [a for a in value if not a.is_inline]


class AttachmentProcessRequest(BaseModel):
    filename: str
    target: str  # "document" | "drawing"


class AttachmentProcessResponse(BaseModel):
    document_id: uuid.UUID
    target: str
    drawing_id: uuid.UUID | None = None
    task_id: str | None = None


class EmailFetchRequest(BaseModel):
    mailbox: str | None = None  # None = all configured mailboxes


class EmailFetchResponse(BaseModel):
    fetched_count: int
    new_messages: list[EmailMessageOut] = []
    errors: list[str] = []
    task_id: str | None = None


# ── Labels ─────────────────────────────────────────────────────────────────


class EmailLabelOut(BaseModel):
    id: uuid.UUID
    name: str
    color: str | None = None
    mailbox: str | None = None
    is_system: bool = False
    thread_count: int = 0

    model_config = {"from_attributes": True}


class EmailLabelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    color: str | None = None
    mailbox: str | None = None


class EmailLabelUpdate(BaseModel):
    name: str | None = None
    color: str | None = None


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
    is_read: bool = False
    is_starred: bool = False
    has_attachments: bool = False
    folder: str = "inbox"
    last_snippet: str | None = None
    unread_count: int = 0
    labels: list[EmailLabelOut] = []
    # Derived-for-list fields (filled by list endpoints, not the ORM):
    sender: str | None = None
    # Готовый черновик ответа ждёт отправки. Агент готовит их сам, но узнать
    # об этом можно было, только открыв переписку.
    has_draft: bool = False
    # С кем переписка с точки зрения читающего. В «Отправленных» и
    # «Черновиках» отправитель — мы сами, и список выглядел как переписка с
    # собой; там нужен получатель.
    counterparty: str | None = None

    model_config = {"from_attributes": True}


class ThreadListResponse(BaseModel):
    items: list[EmailThreadOut]
    total: int
    next_cursor: str | None = None


class BulkThreadAction(BaseModel):
    thread_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)
    action: Literal[
        "read", "unread", "star", "unstar",
        "archive", "trash", "spam", "inbox", "move",
        "add_label", "remove_label",
    ]
    folder: str | None = None
    label_id: uuid.UUID | None = None


class BulkActionResult(BaseModel):
    updated: int


# ── Draft ──────────────────────────────────────────────────────────────────


class EmailDraftCreate(BaseModel):
    to_addresses: list[str]
    cc_addresses: list[str] = []
    bcc_addresses: list[str] = []
    subject: str
    body_html: str
    body_text: str | None = None
    thread_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None
    context: dict | None = None  # invoice_id, document_id, etc.
    in_reply_to_message_id: uuid.UUID | None = None
    forward_of_message_id: uuid.UUID | None = None
    attachment_ids: list[uuid.UUID] = []
    # Which configured mailbox (MailboxConfig.name) this should be SENT from —
    # its SMTP credentials/from-address are used instead of the global .env
    # fallback. Explicit override; when omitted and thread_id is given, the
    # thread's own mailbox is used instead (see create_draft).
    mailbox: str | None = None


class EmailDraftUpdate(BaseModel):
    to_addresses: list[str] | None = None
    cc_addresses: list[str] | None = None
    bcc_addresses: list[str] | None = None
    subject: str | None = None
    body_html: str | None = None
    body_text: str | None = None
    attachment_ids: list[uuid.UUID] | None = None
    mailbox: str | None = None


class EmailDraftAttachment(BaseModel):
    id: uuid.UUID
    filename: str
    size: int | None = None
    content_type: str | None = None


class EmailDraftOut(BaseModel):
    id: uuid.UUID
    to_addresses: list[str]
    cc_addresses: list[str] | None
    bcc_addresses: list[str] | None = None
    subject: str
    body_html: str | None
    body_text: str | None
    thread_id: uuid.UUID | None
    mailbox: str | None = None
    status: str  # draft, risk_checked, approved, sent
    risk_flags: list[dict] = []
    attachment_ids: list[uuid.UUID] = []
    # Имена и размеры вложений черновика. Одних id недостаточно: открыв
    # сохранённый черновик, композер не мог показать вложения (нечего
    # подписать), не показывал их вовсе — и следующее автосохранение стирало
    # их вместе с пустым attachment_ids.
    attachments: list["EmailDraftAttachment"] = []
    # Без этих двух полей повторно открытый черновик ответа терял связь с
    # перепиской и уходил как новое письмо.
    in_reply_to_message_id: uuid.UUID | None = None
    forward_of_message_id: uuid.UUID | None = None
    # Отложенная отправка: когда письмо должно уйти (для папки «Исходящие»).
    send_at: datetime | None = None
    # sha256 of the letter itself — pass it back as ``expected_digest`` when
    # sending, so an approval cannot be spent on rewritten content.
    content_digest: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Compose + send (human) ─────────────────────────────────────────────────


class ComposeSendRequest(BaseModel):
    """Ф4: ``acknowledged_risks`` carries the codes the person chose to send
    anyway, and ``delay_seconds`` is the undo window."""

    mailbox: str
    to_addresses: list[str] = Field(..., min_length=1)
    cc_addresses: list[str] = []
    bcc_addresses: list[str] = []
    subject: str = ""
    body_html: str = ""
    body_text: str | None = None
    in_reply_to_message_id: uuid.UUID | None = None
    forward_of_message_id: uuid.UUID | None = None
    attachment_ids: list[uuid.UUID] = []
    draft_id: uuid.UUID | None = None
    acknowledged_risks: list[str] = []
    delay_seconds: int | None = None
    send_at: datetime | None = None


class EmailSendResult(BaseModel):
    task_id: str | None = None
    draft_id: uuid.UUID
    status: str
    # Present when the send was refused: the person must look and confirm.
    blocked_by: list[dict] = []
    warnings: list[dict] = []
    # When the message can still be recalled (seconds from now).
    undo_seconds: int = 0


class ContactOut(BaseModel):
    email: str
    name: str | None = None


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


# ── Compose assist / generate (agent + human "help") ───────────────────────


class ComposeAssistRequest(BaseModel):
    subject: str = ""
    body: str
    instruction: str = "Улучши формулировки, сохрани смысл"
    thread_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None
    mailbox: str | None = None


class ComposeAssistResponse(BaseModel):
    subject: str
    body_html: str
    body_text: str
    diff: list[dict] = []
    notes: list[str] = []
    tone: str = "formal"


class AgentComposeRequest(BaseModel):
    mailbox: str
    to_addresses: list[str] = Field(..., min_length=1)
    intent: str
    thread_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None
    attachment_ids: list[uuid.UUID] = []
    tone: str | None = None


# ── Attachment recognition (agent) ─────────────────────────────────────────


class AttachmentRecognizeRequest(BaseModel):
    filename: str | None = None
    mode: Literal["ocr", "classify", "extract", "full"] = "full"


class AttachmentRecognitionResult(BaseModel):
    filename: str
    doc_type: str | None = None
    text: str | None = None
    fields: dict | None = None
    confidence: float | None = None
    document_id: uuid.UUID | None = None


class AttachmentRecognizeResponse(BaseModel):
    results: list[AttachmentRecognitionResult] = []


# ── Risk Check ─────────────────────────────────────────────────────────────


class RiskFlag(BaseModel):
    code: str
    severity: str  # warning, error
    message: str
    can_override: bool = True


class RiskCheckRequest(BaseModel):
    body: str | None = None
    subject: str | None = None


class RiskCheckResponse(BaseModel):
    draft_id: uuid.UUID | None = None
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
    folder: str | None = None
    label_ids: list[uuid.UUID] = []
    is_unread: bool | None = None
    is_starred: bool | None = None
    has_attachments: bool | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    from_addr: str | None = None
    to_addr: str | None = None
    sort: Literal["date_desc", "date_asc", "relevance"] = "date_desc"
    cursor: str | None = None
    limit: int = 50


class EmailSearchResponse(BaseModel):
    results: list[EmailMessageOut]
    total: int
    next_cursor: str | None = None
