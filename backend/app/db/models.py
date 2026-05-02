"""SQLAlchemy models — MVP data model (Epic 1)."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import GUID

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


# ── Enums ────────────────────────────────────────────────────────────────────


class DocumentType(str, enum.Enum):
    invoice = "invoice"
    letter = "letter"
    contract = "contract"
    drawing = "drawing"
    commercial_offer = "commercial_offer"
    act = "act"
    waybill = "waybill"
    other = "other"


class DocumentStatus(str, enum.Enum):
    ingested = "ingested"
    classifying = "classifying"
    extracting = "extracting"
    needs_review = "needs_review"
    approved = "approved"
    rejected = "rejected"
    archived = "archived"
    suspicious = "suspicious"  # quarantined — blocked until manual review


class InvoiceStatus(str, enum.Enum):
    draft = "draft"
    needs_review = "needs_review"
    approved = "approved"
    rejected = "rejected"
    paid = "paid"


class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    delegated = "delegated"
    expired = "expired"


class ApprovalActionType(str, enum.Enum):
    invoice_approve = "invoice.approve"
    invoice_reject = "invoice.reject"
    invoice_bulk_delete = "invoice.bulk_delete"
    email_send = "email.send"
    anomaly_resolve = "anomaly.resolve"
    table_apply_diff = "table.apply_diff"
    norm_activate_rule = "norm.activate_rule"
    compare_decide = "compare.decide"
    warehouse_confirm_receipt = "warehouse.confirm_receipt"
    payment_mark_paid = "payment.mark_paid"
    procurement_send_rfq = "procurement.send_rfq"
    bom_approve = "bom.approve"
    bom_create_purchase_request = "bom.create_purchase_request"
    tech_process_plan_approve = "tech.process_plan_approve"
    tech_norm_estimate_approve = "tech.norm_estimate_approve"
    tech_learning_rule_activate = "tech.learning_rule_activate"
    agent_tool_call = "agent.tool_call"


class PartyRole(str, enum.Enum):
    supplier = "supplier"
    buyer = "buyer"
    both = "both"


class ConfidenceReason(str, enum.Enum):
    high_quality_ocr = "high_quality_ocr"
    low_quality_ocr = "low_quality_ocr"
    ambiguous_value = "ambiguous_value"
    missing_field = "missing_field"
    format_mismatch = "format_mismatch"
    arithmetic_error = "arithmetic_error"
    normalization_applied = "normalization_applied"


# ── Documents ────────────────────────────────────────────────────────────────


class Document(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "documents"

    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)

    doc_type: Mapped[DocumentType | None] = mapped_column(Enum(DocumentType))
    doc_type_confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus), default=DocumentStatus.ingested, nullable=False
    )

    source_channel: Mapped[str | None] = mapped_column(String(50))  # email, upload, chat, telegram
    source_email_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("email_messages.id")
    )

    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    # Relationships
    versions: Mapped[list["DocumentVersion"]] = relationship(back_populates="document")
    extractions: Mapped[list["DocumentExtraction"]] = relationship(back_populates="document")
    invoice: Mapped["Invoice | None"] = relationship(back_populates="document")
    links: Mapped[list["DocumentLink"]] = relationship(
        back_populates="document", foreign_keys="DocumentLink.document_id"
    )


class DocumentVersion(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "document_versions"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text)

    document: Mapped["Document"] = relationship(back_populates="versions")


class DocumentExtraction(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "document_extractions"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(50))
    raw_output: Mapped[dict | None] = mapped_column(JSON)
    structured_data: Mapped[dict | None] = mapped_column(JSON)
    overall_confidence: Mapped[float | None] = mapped_column(Float)
    processing_time_ms: Mapped[int | None] = mapped_column(Integer)

    document: Mapped["Document"] = relationship(back_populates="extractions")
    fields: Mapped[list["ExtractionField"]] = relationship(back_populates="extraction")


class ExtractionField(UUIDPrimaryKey, Base):
    __tablename__ = "extraction_fields"

    extraction_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("document_extractions.id"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(String(100), nullable=False)
    field_value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    confidence_reason: Mapped[ConfidenceReason | None] = mapped_column(Enum(ConfidenceReason))
    bbox_page: Mapped[int | None] = mapped_column(Integer)
    bbox_x: Mapped[float | None] = mapped_column(Float)
    bbox_y: Mapped[float | None] = mapped_column(Float)
    bbox_w: Mapped[float | None] = mapped_column(Float)
    bbox_h: Mapped[float | None] = mapped_column(Float)
    human_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    corrected_value: Mapped[str | None] = mapped_column(Text)

    extraction: Mapped["DocumentExtraction"] = relationship(back_populates="fields")


class DocumentLink(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "document_links"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False
    )
    linked_entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    linked_entity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    link_type: Mapped[str] = mapped_column(String(50), default="related")

    document: Mapped["Document"] = relationship(back_populates="links", foreign_keys=[document_id])


# ── Knowledge Graph & Memory ────────────────────────────────────────────────


class KnowledgeNode(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "knowledge_nodes"

    node_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    # document, invoice, drawing, process_plan, operation, material, tool, fixture,
    # machine, supplier, warehouse_item, standard, etc.
    title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    canonical_key: Mapped[str | None] = mapped_column(String(500), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(80), index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list | None] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    created_by: Mapped[str] = mapped_column(String(50), default="system", nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True, index=True
    )
    source_document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document_versions.id"), nullable=True, index=True
    )

    outgoing_edges: Mapped[list["KnowledgeEdge"]] = relationship(
        back_populates="source",
        foreign_keys="KnowledgeEdge.source_node_id",
        cascade="all, delete-orphan",
    )
    incoming_edges: Mapped[list["KnowledgeEdge"]] = relationship(
        back_populates="target",
        foreign_keys="KnowledgeEdge.target_node_id",
        cascade="all, delete-orphan",
    )


class KnowledgeEdge(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "knowledge_edges"

    source_node_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("knowledge_nodes.id"), nullable=False, index=True
    )
    target_node_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("knowledge_nodes.id"), nullable=False, index=True
    )
    edge_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    # mentions, derived_from, same_as, version_of, supersedes, requires, uses_tool,
    # uses_fixture, uses_machine, purchased_from, stored_as, conflicts_with, approved_by.
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True, index=True
    )
    source_document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document_versions.id"), nullable=True, index=True
    )
    evidence_span_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("evidence_spans.id"), nullable=True, index=True
    )
    created_by: Mapped[str] = mapped_column(String(50), default="system", nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    source: Mapped["KnowledgeNode"] = relationship(
        back_populates="outgoing_edges", foreign_keys=[source_node_id]
    )
    target: Mapped["KnowledgeNode"] = relationship(
        back_populates="incoming_edges", foreign_keys=[target_node_id]
    )


class DocumentChunk(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "document_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False, index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document_versions.id"), nullable=True, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    page_number: Mapped[int | None] = mapped_column(Integer)
    bbox_data: Mapped[dict | None] = mapped_column(JSON)
    embedding_id: Mapped[str | None] = mapped_column(String(200), index=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class EvidenceSpan(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "evidence_spans"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False, index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document_versions.id"), nullable=True, index=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document_chunks.id"), nullable=True, index=True
    )
    field_name: Mapped[str | None] = mapped_column(String(120), index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    bbox_data: Mapped[dict | None] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class EntityMention(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "entity_mentions"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False, index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document_versions.id"), nullable=True, index=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document_chunks.id"), nullable=True, index=True
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("knowledge_nodes.id"), nullable=True, index=True
    )
    mention_text: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(80), default="manual", nullable=False)
    evidence_span_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("evidence_spans.id"), nullable=True, index=True
    )
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class MemoryEmbeddingRecord(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "memory_embedding_records"

    content_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # document_chunk, evidence_span.
    content_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True, index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document_versions.id"), nullable=True, index=True
    )
    collection_name: Mapped[str] = mapped_column(String(100), default="memory_chunks", nullable=False)
    point_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    vector_size: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False, index=True)
    # queued, indexed, failed, stale.
    error: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class GraphBuildStatus(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "graph_build_statuses"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False, index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document_versions.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False, index=True)
    # not_started, queued, built, needs_review, failed, stale.
    build_scope: Mapped[str] = mapped_column(String(50), default="compact", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class GraphReviewItem(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "graph_review_items"

    item_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False, index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True, index=True
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("knowledge_nodes.id"), nullable=True, index=True
    )
    edge_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("knowledge_edges.id"), nullable=True, index=True
    )
    mention_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("entity_mentions.id"), nullable=True, index=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    suggested_by: Mapped[str] = mapped_column(String(50), default="system", nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


# ── Invoices ─────────────────────────────────────────────────────────────────


class Invoice(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "invoices"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), unique=True, nullable=False
    )
    invoice_number: Mapped[str | None] = mapped_column(String(100), index=True)
    invoice_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(3), default="RUB")

    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("parties.id")
    )
    buyer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("parties.id")
    )

    subtotal: Mapped[float | None] = mapped_column(Float)
    tax_amount: Mapped[float | None] = mapped_column(Float)
    total_amount: Mapped[float | None] = mapped_column(Float)

    payment_id: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    validity_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus), default=InvoiceStatus.needs_review, nullable=False
    )
    overall_confidence: Mapped[float | None] = mapped_column(Float)

    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    document: Mapped["Document"] = relationship(back_populates="invoice")
    supplier: Mapped["Party | None"] = relationship(foreign_keys=[supplier_id])
    buyer: Mapped["Party | None"] = relationship(foreign_keys=[buyer_id])
    lines: Mapped[list["InvoiceLine"]] = relationship(back_populates="invoice")


class InvoiceLine(UUIDPrimaryKey, Base):
    __tablename__ = "invoice_lines"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("invoices.id"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sku: Mapped[str | None] = mapped_column(String(200), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(50))
    unit_price: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    tax_rate: Mapped[float | None] = mapped_column(Float)
    tax_amount: Mapped[float | None] = mapped_column(Float)
    weight: Mapped[float | None] = mapped_column(Float)

    canonical_item_id: Mapped[uuid.UUID | None] = mapped_column(GUID())

    confidence: Mapped[float | None] = mapped_column(Float)
    bbox_data: Mapped[dict | None] = mapped_column(JSON)
    metadata_: Mapped[dict | None] = mapped_column("metadata_", JSON)

    invoice: Mapped["Invoice"] = relationship(back_populates="lines")


# ── Parties & Suppliers ──────────────────────────────────────────────────────


class Party(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "parties"

    name: Mapped[str] = mapped_column(String(500), nullable=False)
    inn: Mapped[str | None] = mapped_column(String(12), index=True)
    kpp: Mapped[str | None] = mapped_column(String(9))
    ogrn: Mapped[str | None] = mapped_column(String(15))
    address: Mapped[str | None] = mapped_column(Text)
    role: Mapped[PartyRole] = mapped_column(Enum(PartyRole), default=PartyRole.supplier)

    bank_name: Mapped[str | None] = mapped_column(String(500))
    bank_bik: Mapped[str | None] = mapped_column(String(9))
    bank_account: Mapped[str | None] = mapped_column(String(20))
    corr_account: Mapped[str | None] = mapped_column(String(20))

    contact_email: Mapped[str | None] = mapped_column(String(200))
    contact_phone: Mapped[str | None] = mapped_column(String(50))

    user_notes: Mapped[str | None] = mapped_column(Text)
    user_rating: Mapped[int | None] = mapped_column(SmallInteger)

    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    profile: Mapped["SupplierProfile | None"] = relationship(back_populates="party")


class SupplierProfile(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "supplier_profiles"

    party_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("parties.id"), unique=True, nullable=False
    )
    total_invoices: Mapped[int] = mapped_column(Integer, default=0)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    avg_processing_days: Mapped[float | None] = mapped_column(Float)
    last_invoice_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trust_score: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    party: Mapped["Party"] = relationship(back_populates="profile")


# ── Email ────────────────────────────────────────────────────────────────────


class EmailThread(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "email_threads"

    subject: Mapped[str] = mapped_column(String(1000), nullable=False)
    mailbox: Mapped[str] = mapped_column(String(100), nullable=False)  # procurement, accounting, general
    party_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("parties.id")
    )
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list["EmailMessage"]] = relationship(back_populates="thread")


class EmailMessage(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "email_messages"

    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("email_threads.id")
    )
    message_id_header: Mapped[str | None] = mapped_column(String(500), unique=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(500))
    mailbox: Mapped[str] = mapped_column(String(100), nullable=False)

    from_address: Mapped[str] = mapped_column(String(500), nullable=False)
    to_addresses: Mapped[list | None] = mapped_column(JSON)
    cc_addresses: Mapped[list | None] = mapped_column(JSON)
    subject: Mapped[str | None] = mapped_column(String(1000))
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)
    attachments_meta: Mapped[list | None] = mapped_column(JSON)

    is_inbound: Mapped[bool] = mapped_column(Boolean, default=True)

    thread: Mapped["EmailThread | None"] = relationship(back_populates="messages")


# ── Audit ────────────────────────────────────────────────────────────────────


class AuditLog(UUIDPrimaryKey, Base):
    __tablename__ = "audit_logs"

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    details: Mapped[dict | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(45))


class AuditTimelineEvent(UUIDPrimaryKey, Base):
    __tablename__ = "audit_timeline_events"

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(100))  # user or "sveta"
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON)


# ── Approvals ────────────────────────────────────────────────────────────────


class Approval(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "approvals"

    action_type: Mapped[ApprovalActionType] = mapped_column(
        Enum(ApprovalActionType), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)

    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus), default=ApprovalStatus.pending, nullable=False
    )
    requested_by: Mapped[str | None] = mapped_column(String(100))  # "sveta" or user
    assigned_to: Mapped[str | None] = mapped_column(String(100))
    delegated_to: Mapped[str | None] = mapped_column(String(100))

    context: Mapped[dict | None] = mapped_column(JSON)  # preview data for the approval
    decision_comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(100))

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Draft Actions ────────────────────────────────────────────────────────────


class DraftAction(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "draft_actions"

    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID())

    draft_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("approvals.id")
    )
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Interaction ──────────────────────────────────────────────────────────────


class Snooze(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "snoozes"

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)


class Handover(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "handovers"

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    from_user: Mapped[str] = mapped_column(String(100), nullable=False)
    to_user: Mapped[str] = mapped_column(String(100), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)


class Comment(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "comments"

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("comments.id")
    )


# ── Saved Views & Queries ───────────────────────────────────────────────────


class SavedView(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "saved_views"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    filters: Mapped[dict] = mapped_column(JSON, nullable=False)
    columns: Mapped[list | None] = mapped_column(JSON)
    sort_by: Mapped[str | None] = mapped_column(String(100))
    sort_order: Mapped[str] = mapped_column(String(4), default="desc")
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)


# ── Normalization Rules ─────────────────────────────────────────────────────


class NormRuleStatus(str, enum.Enum):
    proposed = "proposed"
    active = "active"
    disabled = "disabled"
    rejected = "rejected"


class NormalizationRule(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "normalization_rules"

    field_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    replacement: Mapped[str] = mapped_column(Text, nullable=False)
    is_regex: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[NormRuleStatus] = mapped_column(
        Enum(NormRuleStatus), default=NormRuleStatus.proposed, nullable=False
    )

    source_corrections: Mapped[int] = mapped_column(Integer, default=0)
    suggested_by: Mapped[str] = mapped_column(String(50), default="system")
    activated_by: Mapped[str | None] = mapped_column(String(100))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    apply_count: Mapped[int] = mapped_column(Integer, default=0)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    description: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class SavedQuery(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "saved_queries"

    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    nl_text: Mapped[str] = mapped_column(Text, nullable=False)
    structured_query: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_count: Mapped[int | None] = mapped_column(Integer)
    is_alert: Mapped[bool] = mapped_column(Boolean, default=False)
    alert_cron: Mapped[str | None] = mapped_column(String(50))


# ── Canonical Items & Price History ────────────────────────────────────────


class CanonicalItem(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "canonical_items"

    name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(200))
    unit: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list | None] = mapped_column(JSON)  # alternative names
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Нормировщик: классификация и стандарты
    okpd2_code: Mapped[str | None] = mapped_column(String(20))
    gost: Mapped[str | None] = mapped_column(String(200))
    hazard_class: Mapped[str | None] = mapped_column(String(10))

    price_history: Mapped[list["PriceHistoryEntry"]] = relationship(back_populates="canonical_item")
    norm_cards: Mapped[list["NormCard"]] = relationship(back_populates="canonical_item")


class PriceHistoryEntry(UUIDPrimaryKey, Base):
    __tablename__ = "price_history_entries"

    canonical_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("canonical_items.id"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("parties.id")
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("invoices.id")
    )
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(GUID())

    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    canonical_item: Mapped["CanonicalItem"] = relationship(back_populates="price_history")


# ── Collections ────────────────────────────────────────────────────────────


class Collection(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "collections"

    name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closure_summary: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    items: Mapped[list["CollectionItem"]] = relationship(back_populates="collection")


class CollectionItem(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "collection_items"

    collection_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)  # document, invoice, email, supplier
    entity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    added_by: Mapped[str] = mapped_column(String(100), default="user")

    collection: Mapped["Collection"] = relationship(back_populates="items")


# ── Anomaly Detection ──────────────────────────────────────────────────────


class AnomalyType(str, enum.Enum):
    duplicate = "duplicate"
    new_supplier = "new_supplier"
    requisite_change = "requisite_change"
    price_spike = "price_spike"
    invoice_email_mismatch = "invoice_email_mismatch"
    unknown_item = "unknown_item"


class AnomalySeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class AnomalyStatus(str, enum.Enum):
    open = "open"
    resolved = "resolved"
    false_positive = "false_positive"
    escalated = "escalated"


class AnomalyCard(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "anomaly_cards"

    anomaly_type: Mapped[AnomalyType] = mapped_column(Enum(AnomalyType), nullable=False)
    severity: Mapped[AnomalySeverity] = mapped_column(
        Enum(AnomalySeverity), default=AnomalySeverity.warning, nullable=False
    )
    status: Mapped[AnomalyStatus] = mapped_column(
        Enum(AnomalyStatus), default=AnomalyStatus.open, nullable=False
    )

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON)

    resolved_by: Mapped[str | None] = mapped_column(String(100))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_comment: Mapped[str | None] = mapped_column(Text)


# ── Compare КП ─────────────────────────────────────────────────────────────


class CompareSession(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "compare_sessions"

    name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="draft")  # draft, aligned, decided
    invoice_ids: Mapped[list] = mapped_column(JSON, nullable=False)  # list of invoice UUIDs
    alignment: Mapped[dict | None] = mapped_column(JSON)  # aligned line items
    decision: Mapped[dict | None] = mapped_column(JSON)  # chosen supplier + reasoning
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Quarantine ─────────────────────────────────────────────────────────────


class FileExtensionAllowlist(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "file_extension_allowlist"

    extension: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    # ".pdf", ".jpg", ".xlsx" etc.
    is_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    added_by: Mapped[str] = mapped_column(String(100), default="system")


class QuarantineEntry(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "quarantine_entries"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False, unique=True
    )
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    # "extension_not_allowed" | "mime_mismatch" | "size_limit_exceeded"
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    detected_mime: Mapped[str | None] = mapped_column(String(100))
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str | None] = mapped_column(String(20))
    # "released" | "deleted"


# ── Agent Actions ──────────────────────────────────────────────────────────


class AgentAction(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "agent_actions"

    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # "llm_call" | "tool_call" | "tool_result" | "approval_request" | "approval_decision"
    tool_name: Mapped[str | None] = mapped_column(String(100))
    tool_args: Mapped[dict | None] = mapped_column(JSON)
    tool_result: Mapped[dict | None] = mapped_column(JSON)
    content_text: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(String(100))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


# ── Document Artifacts & Processing Jobs ───────────────────────────────────


class DocumentArtifact(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "document_artifacts"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False, index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # "preview_png" | "ocr_text" | "extracted_text" | "excel_export" | "thumbnail"
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class DocumentProcessingJob(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "document_processing_jobs"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    # "queued" | "running" | "done" | "failed"
    pipeline_steps: Mapped[list] = mapped_column(JSON, nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    celery_task_id: Mapped[str | None] = mapped_column(String(200))


# ── Export Jobs ─────────────────────────────────────────────────────────────


class ExportJob(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "export_jobs"

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    export_format: Mapped[str] = mapped_column(String(20), nullable=False)
    # "excel" | "1c_xml" | "json" | "csv"
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # "pending" | "generating" | "ready" | "sent" | "failed"
    storage_path: Mapped[str | None] = mapped_column(String(1000))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("approvals.id"))
    requested_by: Mapped[str] = mapped_column(String(100), default="user")
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


# ── Draft Emails ────────────────────────────────────────────────────────────


class DraftEmail(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "draft_emails"

    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("email_threads.id")
    )
    related_entity_type: Mapped[str | None] = mapped_column(String(50))
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    to_addresses: Mapped[list] = mapped_column(JSON, nullable=False)
    cc_addresses: Mapped[list | None] = mapped_column(JSON)
    subject: Mapped[str] = mapped_column(String(1000), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)
    risk_flags: Mapped[list | None] = mapped_column(JSON)
    # [{severity: "warning"|"critical", message: "..."}]
    approval_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("approvals.id"))
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    # "draft" | "pending_approval" | "approved" | "sent" | "cancelled"
    generated_by: Mapped[str] = mapped_column(String(50), default="sveta")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Calendar & Reminders ───────────────────────────────────────────────────


class CalendarEvent(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "calendar_events"

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    event_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)  # due_date, payment, delivery, meeting
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    source: Mapped[str] = mapped_column(String(50), default="extraction")  # extraction, manual, email
    user_id: Mapped[str | None] = mapped_column(String(100))


class Reminder(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "reminders"

    calendar_event_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("calendar_events.id")
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[str] = mapped_column(String(100), default="user")


# ── Warehouse ────────────────────────────────────────────────────────────────


class InventoryItem(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "inventory_items"

    canonical_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("canonical_items.id")
    )
    sku: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    current_qty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    min_qty: Mapped[float | None] = mapped_column(Float)
    location: Mapped[str | None] = mapped_column(String(200))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    stock_movements: Mapped[list["StockMovement"]] = relationship(back_populates="item")


class WarehouseReceipt(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "warehouse_receipts"

    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("invoices.id")
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id")
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("parties.id")
    )
    receipt_number: Mapped[str | None] = mapped_column(String(100), index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    received_by: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    # "draft" | "confirmed" | "cancelled"
    notes: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["WarehouseReceiptLine"]] = relationship(back_populates="receipt")
    invoice: Mapped["Invoice | None"] = relationship(foreign_keys=[invoice_id])
    supplier: Mapped["Party | None"] = relationship(foreign_keys=[supplier_id])


class WarehouseReceiptLine(UUIDPrimaryKey, Base):
    __tablename__ = "warehouse_receipt_lines"

    receipt_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("warehouse_receipts.id"), nullable=False, index=True
    )
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("inventory_items.id")
    )
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("invoice_lines.id")
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity_expected: Mapped[float] = mapped_column(Float, nullable=False)
    quantity_received: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    discrepancy_note: Mapped[str | None] = mapped_column(Text)

    receipt: Mapped["WarehouseReceipt"] = relationship(back_populates="lines")
    inventory_item: Mapped["InventoryItem | None"] = relationship(foreign_keys=[inventory_item_id])


class StockMovement(UUIDPrimaryKey, Base):
    __tablename__ = "stock_movements"

    inventory_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("inventory_items.id"), nullable=False, index=True
    )
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # "receipt" | "issue" | "adjustment" | "write_off" | "transfer"
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    balance_after: Mapped[float] = mapped_column(Float, nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(50))
    reference_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    performed_by: Mapped[str] = mapped_column(String(100), nullable=False)
    performed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    notes: Mapped[str | None] = mapped_column(Text)

    item: Mapped["InventoryItem"] = relationship(back_populates="stock_movements")


# ── Procurement (Этап 4) ──────────────────────────────────────────────────────


class PurchaseRequest(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "purchase_requests"

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False, default="user")
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    # "draft" | "approved" | "rfq_sent" | "offers_received" | "completed" | "cancelled"
    items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # [{name, qty, unit, target_price, canonical_item_id}]
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    compare_session_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("compare_sessions.id")
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("approvals.id")
    )


class SupplierContract(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "supplier_contracts"

    supplier_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("parties.id"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id")
    )
    contract_number: Mapped[str | None] = mapped_column(String(100))
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    # "active" | "expired" | "draft" | "terminated"
    payment_terms: Mapped[str | None] = mapped_column(String(200))
    delivery_terms: Mapped[str | None] = mapped_column(String(200))
    credit_limit: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    supplier: Mapped["Party"] = relationship(foreign_keys=[supplier_id])


# ── Payment Schedule (Этап 5) ─────────────────────────────────────────────────


class PaymentSchedule(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "payment_schedules"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("invoices.id"), nullable=False, index=True
    )
    payment_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    due_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="scheduled", nullable=False)
    # "scheduled" | "paid" | "overdue" | "partial" | "cancelled"
    payment_method: Mapped[str | None] = mapped_column(String(50))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_amount: Mapped[float | None] = mapped_column(Float)
    reference: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    invoice: Mapped["Invoice"] = relationship(foreign_keys=[invoice_id])


# ── Normalization: NormCard (Этап 6) ─────────────────────────────────────────


class NormCard(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "norm_cards"

    canonical_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("canonical_items.id"), nullable=False, index=True
    )
    product_code: Mapped[str | None] = mapped_column(String(100))
    norm_qty: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    loss_factor: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    # 1.05 = 5% отходы/потери
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)

    canonical_item: Mapped["CanonicalItem"] = relationship(back_populates="norm_cards")


# ── BOM — Bill of Materials (Этап 7) ─────────────────────────────────────────


class BOM(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "boms"

    product_name: Mapped[str] = mapped_column(String(500), nullable=False)
    product_code: Mapped[str | None] = mapped_column(String(100), unique=True)
    version: Mapped[str] = mapped_column(String(50), default="1.0", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    # "draft" | "approved" | "obsolete"
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id")
    )
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["BOMLine"]] = relationship(back_populates="bom", order_by="BOMLine.line_number")


class BOMLine(UUIDPrimaryKey, Base):
    __tablename__ = "bom_lines"

    bom_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("boms.id"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("canonical_items.id")
    )
    norm_card_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("norm_cards.id")
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    bom: Mapped["BOM"] = relationship(back_populates="lines")


# ── Manufacturing Technology ────────────────────────────────────────────────


class ManufacturingResource(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "manufacturing_resources"

    resource_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # machine, tool, fixture, measuring_tool, workplace, auxiliary_equipment.
    name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    code: Mapped[str | None] = mapped_column(String(100), index=True)
    model: Mapped[str | None] = mapped_column(String(200))
    standard: Mapped[str | None] = mapped_column(String(200))
    capabilities: Mapped[dict | None] = mapped_column(JSON)
    location: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class ManufacturingProcessPlan(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "manufacturing_process_plans"

    document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True, index=True
    )
    bom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("boms.id"), nullable=True, index=True
    )
    product_name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    product_code: Mapped[str | None] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(50), default="1.0", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False, index=True)
    standard_system: Mapped[str] = mapped_column(String(100), default="ЕСТД", nullable=False)
    route_summary: Mapped[str | None] = mapped_column(Text)
    material: Mapped[str | None] = mapped_column(String(300))
    blank_type: Mapped[str | None] = mapped_column(String(300))
    quality_requirements: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(100), default="sveta", nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    operations: Mapped[list["ManufacturingOperation"]] = relationship(
        back_populates="process_plan",
        cascade="all, delete-orphan",
        order_by="ManufacturingOperation.sequence_no",
    )
    norm_estimates: Mapped[list["ManufacturingNormEstimate"]] = relationship(
        back_populates="process_plan",
        cascade="all, delete-orphan",
    )


class ManufacturingOperation(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "manufacturing_operations"

    process_plan_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("manufacturing_process_plans.id"), nullable=False, index=True
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_code: Mapped[str | None] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    operation_type: Mapped[str | None] = mapped_column(String(100), index=True)
    machine_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("manufacturing_resources.id"), nullable=True, index=True
    )
    tool_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("manufacturing_resources.id"), nullable=True, index=True
    )
    fixture_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("manufacturing_resources.id"), nullable=True, index=True
    )
    setup_description: Mapped[str | None] = mapped_column(Text)
    transition_text: Mapped[str | None] = mapped_column(Text)
    cutting_parameters: Mapped[dict | None] = mapped_column(JSON)
    control_requirements: Mapped[str | None] = mapped_column(Text)
    safety_requirements: Mapped[str | None] = mapped_column(Text)
    setup_minutes: Mapped[float | None] = mapped_column(Float)
    machine_minutes: Mapped[float | None] = mapped_column(Float)
    labor_minutes: Mapped[float | None] = mapped_column(Float)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    process_plan: Mapped["ManufacturingProcessPlan"] = relationship(back_populates="operations")
    machine_resource: Mapped["ManufacturingResource | None"] = relationship(
        foreign_keys=[machine_resource_id]
    )
    tool_resource: Mapped["ManufacturingResource | None"] = relationship(
        foreign_keys=[tool_resource_id]
    )
    fixture_resource: Mapped["ManufacturingResource | None"] = relationship(
        foreign_keys=[fixture_resource_id]
    )


class ManufacturingNormEstimate(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "manufacturing_norm_estimates"

    process_plan_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("manufacturing_process_plans.id"), nullable=False, index=True
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("manufacturing_operations.id"), nullable=True, index=True
    )
    setup_minutes: Mapped[float | None] = mapped_column(Float)
    machine_minutes: Mapped[float | None] = mapped_column(Float)
    labor_minutes: Mapped[float | None] = mapped_column(Float)
    batch_size: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    method: Mapped[str] = mapped_column(String(100), default="manual", nullable=False)
    assumptions: Mapped[list | None] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(100), default="sveta", nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    process_plan: Mapped["ManufacturingProcessPlan"] = relationship(
        back_populates="norm_estimates"
    )
    operation: Mapped["ManufacturingOperation | None"] = relationship(
        foreign_keys=[operation_id]
    )


class ManufacturingOperationTemplate(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "manufacturing_operation_templates"

    operation_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    standard_system: Mapped[str] = mapped_column(String(100), default="ЕСТД", nullable=False)
    default_operation_code: Mapped[str | None] = mapped_column(String(50))
    required_resource_types: Mapped[list | None] = mapped_column(JSON)
    default_transition_text: Mapped[str | None] = mapped_column(Text)
    default_control_requirements: Mapped[str | None] = mapped_column(Text)
    default_safety_requirements: Mapped[str | None] = mapped_column(Text)
    parameters_schema: Mapped[dict | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class ManufacturingCheckResult(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "manufacturing_check_results"

    process_plan_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("manufacturing_process_plans.id"), nullable=False, index=True
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("manufacturing_operations.id"), nullable=True, index=True
    )
    check_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="open", nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(100), default="system", nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(String(100))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class TechnologyCorrection(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "technology_corrections"

    entity_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    correction_type: Mapped[str] = mapped_column(String(80), default="manual_edit", nullable=False)
    corrected_by: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True, index=True
    )
    process_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("manufacturing_process_plans.id"), nullable=True, index=True
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("manufacturing_operations.id"), nullable=True, index=True
    )
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class TechnologyLearningRule(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "technology_learning_rules"

    rule_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    match_old_value: Mapped[str | None] = mapped_column(Text)
    replacement_value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="proposed", nullable=False, index=True)
    suggested_by: Mapped[str] = mapped_column(String(100), default="system", nullable=False)
    activated_by: Mapped[str | None] = mapped_column(String(100))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


# ── NTD / Norm Control ───────────────────────────────────────────────────────


class NTDControlSettings(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "ntd_control_settings"

    singleton_key: Mapped[str] = mapped_column(String(50), default="default", unique=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    # manual, auto.
    updated_by: Mapped[str | None] = mapped_column(String(100))


class NormativeDocument(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "normative_documents"

    code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    document_type: Mapped[str] = mapped_column(String(50), default="ГОСТ", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False, index=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    scope: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True, index=True
    )
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    versions: Mapped[list["NormativeDocumentVersion"]] = relationship(
        back_populates="normative_document",
        cascade="all, delete-orphan",
        foreign_keys="NormativeDocumentVersion.normative_document_id",
    )
    clauses: Mapped[list["NormativeClause"]] = relationship(
        back_populates="normative_document",
        cascade="all, delete-orphan",
    )
    requirements: Mapped[list["NormativeRequirement"]] = relationship(
        back_populates="normative_document",
        cascade="all, delete-orphan",
    )


class NormativeDocumentVersion(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "normative_document_versions"

    normative_document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("normative_documents.id"), nullable=False, index=True
    )
    version_label: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    effective_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False, index=True)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True, index=True
    )
    text_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    normative_document: Mapped["NormativeDocument"] = relationship(
        back_populates="versions",
        foreign_keys=[normative_document_id],
    )


class NormativeClause(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "normative_clauses"

    normative_document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("normative_documents.id"), nullable=False, index=True
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("normative_document_versions.id"), nullable=True, index=True
    )
    parent_clause_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("normative_clauses.id"), nullable=True, index=True
    )
    clause_number: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(500), index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    normative_document: Mapped["NormativeDocument"] = relationship(back_populates="clauses")


class NormativeRequirement(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "normative_requirements"

    normative_document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("normative_documents.id"), nullable=False, index=True
    )
    clause_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("normative_clauses.id"), nullable=True, index=True
    )
    requirement_code: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    requirement_type: Mapped[str] = mapped_column(String(80), default="generic", nullable=False, index=True)
    applies_to: Mapped[list | None] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    required_keywords: Mapped[list | None] = mapped_column(JSON)
    severity: Mapped[str] = mapped_column(String(30), default="warning", nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    normative_document: Mapped["NormativeDocument"] = relationship(back_populates="requirements")


class NTDCheckRun(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "ntd_check_runs"

    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False, index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document_versions.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="completed", nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="manual", nullable=False, index=True)
    triggered_by: Mapped[str] = mapped_column(String(20), default="manual", nullable=False, index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    findings_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    findings_open: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    findings: Mapped[list["NTDCheckFinding"]] = relationship(
        back_populates="check",
        cascade="all, delete-orphan",
    )


class NTDCheckFinding(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "ntd_check_findings"

    check_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("ntd_check_runs.id"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=False, index=True
    )
    normative_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("normative_documents.id"), nullable=True, index=True
    )
    clause_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("normative_clauses.id"), nullable=True, index=True
    )
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("normative_requirements.id"), nullable=True, index=True
    )
    severity: Mapped[str] = mapped_column(String(30), default="warning", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="open", nullable=False, index=True)
    finding_code: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_text: Mapped[str | None] = mapped_column(Text)
    recommendation: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    check: Mapped["NTDCheckRun"] = relationship(back_populates="findings")
