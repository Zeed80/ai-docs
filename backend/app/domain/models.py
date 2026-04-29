from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.base import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class CaseStatus(str, Enum):
    NEW = "new"
    IN_REVIEW = "in_review"
    ACTIVE = "active"
    DONE = "done"
    ARCHIVED = "archived"


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    SUSPICIOUS = "suspicious"
    PROCESSING = "processing"
    PROCESSED = "processed"
    PROCESSING_FAILED = "processing_failed"
    CLASSIFIED = "classified"
    EXTRACTED = "extracted"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class AuditEventType(str, Enum):
    CASE_CREATED = "case_created"
    CASE_UPDATED = "case_updated"
    DOCUMENT_UPLOADED = "document_uploaded"
    DOCUMENT_PROCESSING_STARTED = "document_processing_started"
    DOCUMENT_PROCESSING_COMPLETED = "document_processing_completed"
    DOCUMENT_PROCESSING_FAILED = "document_processing_failed"
    DOCUMENT_ARTIFACT_CREATED = "document_artifact_created"
    DOCUMENT_QUARANTINED = "document_quarantined"
    DOCUMENT_CLASSIFIED = "document_classified"
    DOCUMENT_EXTRACTED = "document_extracted"
    DRAWING_ANALYZED = "drawing_analyzed"
    CUSTOMER_QUESTION_DRAFTED = "customer_question_drafted"
    INVOICE_EXTRACTED = "invoice_extracted"
    INVOICE_ANOMALY_CREATED = "invoice_anomaly_created"
    SUPPLIER_REQUISITES_DIFF_DETECTED = "supplier_requisites_diff_detected"
    INVOICE_EXCEL_EXPORTED = "invoice_excel_exported"
    ONEC_EXPORT_PREPARED = "onec_export_prepared"
    EMAIL_THREAD_CREATED = "email_thread_created"
    EMAIL_MESSAGE_INGESTED = "email_message_ingested"
    EMAIL_DRAFT_CREATED = "email_draft_created"
    EMAIL_SEND_BLOCKED_FOR_APPROVAL = "email_send_blocked_for_approval"
    AGENT_ACTION_RECORDED = "agent_action_recorded"
    AGENT_SCENARIO_STARTED = "agent_scenario_started"
    AGENT_SCENARIO_COMPLETED = "agent_scenario_completed"
    APPROVAL_GATE_CREATED = "approval_gate_created"
    APPROVAL_GATE_APPROVED = "approval_gate_approved"
    APPROVAL_GATE_REJECTED = "approval_gate_rejected"
    APPROVAL_GATE_EXECUTED = "approval_gate_executed"
    SIGNED_FILE_URL_CREATED = "signed_file_url_created"
    TASK_JOB_CREATED = "task_job_created"
    TASK_JOB_STARTED = "task_job_started"
    TASK_JOB_COMPLETED = "task_job_completed"
    TASK_JOB_FAILED = "task_job_failed"
    TASK_JOB_RETRY_SCHEDULED = "task_job_retry_scheduled"
    TASK_JOB_DEAD_LETTERED = "task_job_dead_lettered"


class ProcessingJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class TaskJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class ManufacturingCase(Base):
    __tablename__ = "manufacturing_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=CaseStatus.NEW.value, nullable=False)
    priority: Mapped[str] = mapped_column(String(50), default="normal", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    documents: Mapped[list[Document]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    drawings: Mapped[list[Drawing]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    process_plans: Mapped[list[ProcessPlan]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    norm_estimates: Mapped[list[NormEstimate]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    quotes: Mapped[list[Quote]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    invoices: Mapped[list[Invoice]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    email_threads: Mapped[list[EmailThread]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    agent_actions: Mapped[list[AgentAction]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    approval_gates: Mapped[list[ApprovalGate]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    task_jobs: Mapped[list[TaskJob]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("manufacturing_cases.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), default=DocumentStatus.UPLOADED.value, nullable=False
    )
    document_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    case: Mapped[ManufacturingCase] = relationship(back_populates="documents")
    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    processing_jobs: Mapped[list[DocumentProcessingJob]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list[DocumentArtifact]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    drawings: Mapped[list[Drawing]] = relationship(back_populates="document")
    quotes: Mapped[list[Quote]] = relationship(back_populates="document")
    invoices: Mapped[list[Invoice]] = relationship(back_populates="document")
    task_jobs: Mapped[list[TaskJob]] = relationship(back_populates="document")


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    document: Mapped[Document] = relationship(back_populates="versions")


class DocumentProcessingJob(Base):
    __tablename__ = "document_processing_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), default=ProcessingJobStatus.PENDING.value, nullable=False
    )
    parser_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped[Document] = relationship(back_populates="processing_jobs")


class DocumentArtifact(Base):
    __tablename__ = "document_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    document: Mapped[Document] = relationship(back_populates="artifacts")


class Drawing(Base):
    __tablename__ = "drawings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("manufacturing_cases.id"), nullable=False)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    drawing_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revision: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)
    material_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    analysis_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    case: Mapped[ManufacturingCase] = relationship(back_populates="drawings")
    document: Mapped[Document | None] = relationship(back_populates="drawings")
    features: Mapped[list[DrawingFeature]] = relationship(
        back_populates="drawing", cascade="all, delete-orphan"
    )
    process_plans: Mapped[list[ProcessPlan]] = relationship(back_populates="drawing")


class DrawingFeature(Base):
    __tablename__ = "drawing_features"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    drawing_id: Mapped[str] = mapped_column(ForeignKey("drawings.id"), nullable=False)
    feature_type: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tolerance: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    drawing: Mapped[Drawing] = relationship(back_populates="features")


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    grade: Mapped[str | None] = mapped_column(String(100), nullable=True)
    standard: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    machine_type: Mapped[str] = mapped_column(String(100), nullable=False)
    capabilities_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    operations: Mapped[list[Operation]] = relationship(back_populates="machine")


class Tool(Base):
    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_type: Mapped[str] = mapped_column(String(100), nullable=False)
    diameter_mm: Mapped[str | None] = mapped_column(String(50), nullable=True)
    material: Mapped[str | None] = mapped_column(String(100), nullable=True)
    coating: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    operations: Mapped[list[Operation]] = relationship(back_populates="tool")


class ProcessPlan(Base):
    __tablename__ = "process_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("manufacturing_cases.id"), nullable=False)
    drawing_id: Mapped[str | None] = mapped_column(ForeignKey("drawings.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    case: Mapped[ManufacturingCase] = relationship(back_populates="process_plans")
    drawing: Mapped[Drawing | None] = relationship(back_populates="process_plans")
    operations: Mapped[list[Operation]] = relationship(
        back_populates="process_plan", cascade="all, delete-orphan"
    )
    norm_estimates: Mapped[list[NormEstimate]] = relationship(back_populates="process_plan")


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    process_plan_id: Mapped[str] = mapped_column(ForeignKey("process_plans.id"), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    machine_id: Mapped[str | None] = mapped_column(ForeignKey("machines.id"), nullable=True)
    tool_id: Mapped[str | None] = mapped_column(ForeignKey("tools.id"), nullable=True)
    setup_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    parameters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    process_plan: Mapped[ProcessPlan] = relationship(back_populates="operations")
    machine: Mapped[Machine | None] = relationship(back_populates="operations")
    tool: Mapped[Tool | None] = relationship(back_populates="operations")


class NormEstimate(Base):
    __tablename__ = "norm_estimates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("manufacturing_cases.id"), nullable=False)
    process_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("process_plans.id"), nullable=True
    )
    labor_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    machine_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    setup_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    assumptions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    case: Mapped[ManufacturingCase] = relationship(back_populates="norm_estimates")
    process_plan: Mapped[ProcessPlan | None] = relationship(back_populates="norm_estimates")


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    inn: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kpp: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bank_details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    quotes: Mapped[list[Quote]] = relationship(back_populates="supplier")
    invoices: Mapped[list[Invoice]] = relationship(back_populates="supplier")
    price_history_entries: Mapped[list[PriceHistoryEntry]] = relationship(back_populates="supplier")


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("manufacturing_cases.id"), nullable=False)
    supplier_id: Mapped[str | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    quote_number: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    quote_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str] = mapped_column(String(16), default="RUB", nullable=False)
    total_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)
    extraction_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    case: Mapped[ManufacturingCase] = relationship(back_populates="quotes")
    supplier: Mapped[Supplier | None] = relationship(back_populates="quotes")
    document: Mapped[Document | None] = relationship(back_populates="quotes")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("manufacturing_cases.id"), nullable=False)
    supplier_id: Mapped[str | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    invoice_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str] = mapped_column(String(16), default="RUB", nullable=False)
    subtotal_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="extracted", nullable=False)
    arithmetic_ok: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    duplicate_status: Mapped[str] = mapped_column(String(50), default="not_checked", nullable=False)
    extraction_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    case: Mapped[ManufacturingCase] = relationship(back_populates="invoices")
    supplier: Mapped[Supplier | None] = relationship(back_populates="invoices")
    document: Mapped[Document | None] = relationship(back_populates="invoices")
    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    price_history_entries: Mapped[list[PriceHistoryEntry]] = relationship(back_populates="invoice")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    sku: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    line_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_rate: Mapped[str | None] = mapped_column(String(50), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
    price_history_entries: Mapped[list[PriceHistoryEntry]] = relationship(back_populates="invoice_line")


class PriceHistoryEntry(Base):
    __tablename__ = "price_history_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    supplier_id: Mapped[str | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    invoice_line_id: Mapped[str | None] = mapped_column(ForeignKey("invoice_lines.id"), nullable=True)
    item_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(16), default="RUB", nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    supplier: Mapped[Supplier | None] = relationship(back_populates="price_history_entries")
    invoice: Mapped[Invoice | None] = relationship(back_populates="price_history_entries")
    invoice_line: Mapped[InvoiceLine | None] = relationship(back_populates="price_history_entries")


class EmailThread(Base):
    __tablename__ = "email_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("manufacturing_cases.id"), nullable=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    external_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(50), default="open", nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    case: Mapped[ManufacturingCase | None] = relationship(back_populates="email_threads")
    messages: Mapped[list[EmailMessage]] = relationship(
        back_populates="thread", cascade="all, delete-orphan"
    )
    draft_emails: Mapped[list[DraftEmail]] = relationship(
        back_populates="thread", cascade="all, delete-orphan"
    )


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    thread_id: Mapped[str] = mapped_column(ForeignKey("email_threads.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    sender: Mapped[str] = mapped_column(String(255), nullable=False)
    recipients_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    thread: Mapped[EmailThread] = relationship(back_populates="messages")
    attachments: Mapped[list[EmailAttachment]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class EmailAttachment(Base):
    __tablename__ = "email_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    message_id: Mapped[str] = mapped_column(ForeignKey("email_messages.id"), nullable=False)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    message: Mapped[EmailMessage] = relationship(back_populates="attachments")
    document: Mapped[Document | None] = relationship()


class DraftEmail(Base):
    __tablename__ = "draft_emails"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("email_threads.id"), nullable=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("manufacturing_cases.id"), nullable=True)
    to_json: Mapped[str] = mapped_column(Text, nullable=False)
    cc_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)
    risk_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_required: Mapped[str] = mapped_column(String(10), default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    thread: Mapped[EmailThread | None] = relationship(back_populates="draft_emails")


class AgentAction(Base):
    __tablename__ = "agent_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("manufacturing_cases.id"), nullable=True)
    scenario: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="recorded", nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    case: Mapped[ManufacturingCase | None] = relationship(back_populates="agent_actions")


class ApprovalGate(Base):
    __tablename__ = "approval_gates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("manufacturing_cases.id"), nullable=True)
    action_id: Mapped[str | None] = mapped_column(ForeignKey("agent_actions.id"), nullable=True)
    gate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    case: Mapped[ManufacturingCase | None] = relationship(back_populates="approval_gates")
    action: Mapped[AgentAction | None] = relationship()


class TaskJob(Base):
    __tablename__ = "task_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(50), default=TaskJobStatus.PENDING.value, nullable=False, index=True
    )
    case_id: Mapped[str | None] = mapped_column(ForeignKey("manufacturing_cases.id"), nullable=True)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    agent_action_id: Mapped[str | None] = mapped_column(ForeignKey("agent_actions.id"), nullable=True)
    approval_gate_id: Mapped[str | None] = mapped_column(ForeignKey("approval_gates.id"), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    case: Mapped[ManufacturingCase | None] = relationship(back_populates="task_jobs")
    document: Mapped[Document | None] = relationship(back_populates="task_jobs")
    agent_action: Mapped[AgentAction | None] = relationship()
    approval_gate: Mapped[ApprovalGate | None] = relationship()


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("manufacturing_cases.id"))
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"))
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), default="system", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    case: Mapped[ManufacturingCase | None] = relationship(back_populates="audit_events")
    document: Mapped[Document | None] = relationship(back_populates="audit_events")
