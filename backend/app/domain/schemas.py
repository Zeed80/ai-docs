from __future__ import annotations

from datetime import datetime

from typing import Any

from pydantic import BaseModel, Field


class CaseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    customer_name: str | None = None
    priority: str = "normal"


class CaseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    customer_name: str | None = None
    status: str | None = None
    priority: str | None = None


class CaseRead(BaseModel):
    id: str
    title: str
    description: str | None
    customer_name: str | None
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime
    document_count: int = 0

    model_config = {"from_attributes": True}


class DocumentArtifactRead(BaseModel):
    id: str | None = None
    document_id: str | None = None
    artifact_type: str
    storage_path: str
    content_type: str | None = None
    page_number: int | None = None
    width: int | None = None
    height: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class DocumentRead(BaseModel):
    id: str
    case_id: str
    filename: str
    content_type: str | None
    sha256: str
    size_bytes: int
    storage_path: str
    status: str
    document_type: str | None
    extracted_text: str | None
    extraction_result: dict[str, Any] | None = None
    ai_summary: str | None
    artifacts: list[DocumentArtifactRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ExtractedField(BaseModel):
    name: str = Field(min_length=1)
    value: str | int | float | bool | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    source: str | None = None


class StructuredDocumentExtraction(BaseModel):
    document_type: str = "unknown"
    summary: str = ""
    fields: list[ExtractedField] = Field(default_factory=list)


class DocumentExtractionResult(BaseModel):
    status: str
    parser_name: str
    text_preview: str | None = None
    text_length: int = 0
    unsupported_reason: str | None = None
    artifacts: list[DocumentArtifactRead] = Field(default_factory=list)
    structured: StructuredDocumentExtraction | None = None


class DocumentProcessingJobRead(BaseModel):
    id: str
    document_id: str
    status: str
    parser_name: str | None
    error_message: str | None
    result: DocumentExtractionResult | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    document: DocumentRead

    model_config = {"from_attributes": True}


class TaskJobRead(BaseModel):
    id: str
    task_type: str
    status: str
    case_id: str | None = None
    document_id: str | None = None
    agent_action_id: str | None = None
    approval_gate_id: str | None = None
    attempt_count: int
    max_attempts: int
    not_before: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class SignedFileUrlRead(BaseModel):
    url: str
    expires_at: int
    filename: str
    content_type: str | None = None


class AuditEventRead(BaseModel):
    id: str
    case_id: str | None
    document_id: str | None
    event_type: str
    actor: str
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentClassifyRequest(BaseModel):
    prompt: str | None = None


class DocumentExtractRequest(BaseModel):
    extraction_goal: str = "Extract key manufacturing and document metadata."


class AIActionRead(BaseModel):
    document: DocumentRead
    ai_text: str | None


class AuthUserRead(BaseModel):
    subject: str
    email: str | None = None
    name: str | None = None
    roles: list[str] = Field(default_factory=list)
    auth_mode: str = "oidc"


class DrawingFeatureCandidate(BaseModel):
    feature_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    dimensions: dict[str, Any] = Field(default_factory=dict)
    tolerance: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class DrawingAnalysisResult(BaseModel):
    title: str = "Untitled drawing"
    drawing_number: str | None = None
    revision: str | None = None
    material_hint: str | None = None
    summary: str = ""
    unclear_items: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    features: list[DrawingFeatureCandidate] = Field(default_factory=list)


class DrawingFeatureRead(BaseModel):
    id: str
    drawing_id: str
    feature_type: str
    description: str
    dimensions: dict[str, Any] = Field(default_factory=dict)
    tolerance: str | None
    confidence: float
    reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DrawingRead(BaseModel):
    id: str
    case_id: str
    document_id: str | None
    title: str
    drawing_number: str | None
    revision: str | None
    status: str
    material_hint: str | None
    analysis: dict[str, Any] | None = None
    features: list[DrawingFeatureRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DrawingAnalysisRead(BaseModel):
    drawing: DrawingRead
    analysis: DrawingAnalysisResult


class CustomerQuestionDraft(BaseModel):
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    questions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    approval_required: bool = True


class CustomerQuestionDraftRead(BaseModel):
    drawing: DrawingRead
    draft: CustomerQuestionDraft


class SupplierExtracted(BaseModel):
    name: str = Field(min_length=1)
    inn: str | None = None
    kpp: str | None = None
    bank_details: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class InvoiceLineExtracted(BaseModel):
    line_no: int | None = None
    description: str = Field(min_length=1)
    sku: str | None = None
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    line_total: float | None = None
    tax_rate: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class InvoiceExtractionResult(BaseModel):
    document_type: str = "invoice"
    supplier: SupplierExtracted
    invoice_number: str | None = None
    invoice_date: str | None = None
    currency: str = "RUB"
    subtotal_amount: float | None = None
    tax_amount: float | None = None
    total_amount: float | None = None
    lines: list[InvoiceLineExtracted] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class InvoiceCheckResult(BaseModel):
    arithmetic_ok: bool
    duplicate_by_hash: bool = False
    duplicate_by_supplier_number: bool = False
    supplier_requisites_diff: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SupplierRead(BaseModel):
    id: str
    name: str
    inn: str | None
    kpp: str | None
    bank_details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class InvoiceLineRead(BaseModel):
    id: str
    invoice_id: str
    line_no: int
    description: str
    sku: str | None
    quantity: float | None
    unit: str | None
    unit_price: float | None
    line_total: float | None
    tax_rate: str | None
    confidence: float
    reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class InvoiceRead(BaseModel):
    id: str
    case_id: str
    supplier: SupplierRead | None
    document_id: str | None
    invoice_number: str | None
    invoice_date: str | None
    currency: str
    subtotal_amount: float | None
    tax_amount: float | None
    total_amount: float | None
    status: str
    arithmetic_ok: str
    duplicate_status: str
    extraction: dict[str, Any] | None = None
    lines: list[InvoiceLineRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class InvoiceAnomalyCard(BaseModel):
    severity: str = "low"
    title: str
    signals: list[str] = Field(default_factory=list)
    recommended_action: str
    approval_required: bool = True


class InvoiceExtractionRead(BaseModel):
    invoice: InvoiceRead
    extraction: InvoiceExtractionResult
    checks: InvoiceCheckResult
    anomaly_card: InvoiceAnomalyCard | None = None


class InvoiceExportRead(BaseModel):
    invoice: InvoiceRead
    artifact: DocumentArtifactRead


class OneCExportRead(BaseModel):
    invoice: InvoiceRead
    payload: dict[str, Any]
    status: str = "prepared"
    approval_required: bool = True


class EmailAttachmentRead(BaseModel):
    id: str
    message_id: str
    document_id: str | None
    filename: str
    content_type: str | None
    storage_path: str | None
    size_bytes: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EmailMessageCreate(BaseModel):
    sender: str = Field(min_length=1)
    recipients: list[str] = Field(default_factory=list)
    subject: str = Field(min_length=1)
    body_text: str | None = None
    external_message_id: str | None = None
    received_at: datetime | None = None


class EmailThreadCreate(BaseModel):
    case_id: str | None = None
    subject: str = Field(min_length=1)
    external_thread_id: str | None = None
    message: EmailMessageCreate | None = None


class EmailMessageRead(BaseModel):
    id: str
    thread_id: str
    direction: str
    external_message_id: str | None
    sender: str
    recipients: list[str] = Field(default_factory=list)
    subject: str
    body_text: str | None
    received_at: datetime | None
    attachments: list[EmailAttachmentRead] = Field(default_factory=list)
    created_at: datetime

    model_config = {"from_attributes": True}


class EmailThreadRead(BaseModel):
    id: str
    case_id: str | None
    subject: str
    external_thread_id: str | None
    status: str
    last_message_at: datetime | None
    messages: list[EmailMessageRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DraftEmailCreate(BaseModel):
    thread_id: str | None = None
    case_id: str | None = None
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = Field(min_length=1)
    body_text: str = Field(min_length=1)


class DraftEmailRead(BaseModel):
    id: str
    thread_id: str | None
    case_id: str | None
    to: list[str]
    cc: list[str]
    subject: str
    body_text: str
    status: str
    risk: dict[str, Any] = Field(default_factory=dict)
    approval_required: bool = True
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EmailSendAttemptRead(BaseModel):
    draft: DraftEmailRead
    status: str = "blocked_for_approval"
    approval_required: bool = True
    reason: str


class ImapPollRead(BaseModel):
    status: str = "placeholder"
    imported_count: int = 0
    approval_required: bool = False
    message: str


class AgentToolSpecRead(BaseModel):
    name: str
    method: str
    path: str
    approval_required: bool = False
    description: str


class AgentScenarioRunRequest(BaseModel):
    case_id: str | None = None
    document_id: str | None = None
    draft_id: str | None = None
    invoice_id: str | None = None
    requested_tools: list[str] | None = None


class AgentActionRead(BaseModel):
    id: str
    case_id: str | None
    scenario: str
    tool_name: str
    status: str
    step_no: int
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    model_config = {"from_attributes": True}


class ApprovalGateRead(BaseModel):
    id: str
    case_id: str | None
    action_id: str | None
    gate_type: str
    status: str
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    decided_at: datetime | None

    model_config = {"from_attributes": True}


class ApprovalGateDecisionRequest(BaseModel):
    actor: str = "system"
    reason: str = Field(min_length=1)


class ApprovalGateDecisionRead(BaseModel):
    approval_gate: ApprovalGateRead
    task: TaskJobRead | None = None


class AgentScenarioRunRead(BaseModel):
    scenario: str
    status: str
    max_steps: int
    actions: list[AgentActionRead] = Field(default_factory=list)
    approval_gates: list[ApprovalGateRead] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
