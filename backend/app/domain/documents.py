"""Pydantic schemas for Document domain — skill contracts."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models import DocumentStatus, DocumentType

# ── Shared ───────────────────────────────────────────────────────────────────


class DocumentBase(BaseModel):
    file_name: str
    doc_type: DocumentType | None = None
    status: DocumentStatus = DocumentStatus.ingested
    source_channel: str | None = None
    metadata_: dict | None = Field(None, alias="metadata")


# ── Ingest (doc.ingest) ─────────────────────────────────────────────────────


class DocumentIngestResponse(BaseModel):
    id: uuid.UUID
    file_name: str
    file_hash: str
    file_size: int
    mime_type: str
    status: DocumentStatus
    is_duplicate: bool = False
    duplicate_of: uuid.UUID | None = None
    quarantined: bool = False
    pipeline_queued: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Get (doc.get) ───────────────────────────────────────────────────────────


class ExtractionFieldOut(BaseModel):
    field_name: str
    field_value: str | None
    confidence: float | None
    confidence_reason: str | None
    bbox_page: int | None = None
    bbox_x: float | None = None
    bbox_y: float | None = None
    bbox_w: float | None = None
    bbox_h: float | None = None
    human_corrected: bool = False
    corrected_value: str | None = None

    model_config = {"from_attributes": True}


class DocumentExtractionOut(BaseModel):
    id: uuid.UUID
    model_name: str
    overall_confidence: float | None
    fields: list[ExtractionFieldOut] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentLinkOut(BaseModel):
    id: uuid.UUID
    linked_entity_type: str
    linked_entity_id: uuid.UUID
    link_type: str

    model_config = {"from_attributes": True}


class DocumentLinkUpdate(BaseModel):
    linked_entity_type: str | None = None
    linked_entity_id: uuid.UUID | None = None
    link_type: str | None = None


class DocumentDependencyNode(BaseModel):
    id: uuid.UUID
    node_type: str
    title: str
    canonical_key: str | None = None
    summary: str | None = None
    confidence: float
    source_document_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class DocumentDependencyEdge(BaseModel):
    id: uuid.UUID
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    edge_type: str
    confidence: float
    reason: str | None = None
    source_document_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class DocumentDependenciesResponse(BaseModel):
    document_id: uuid.UUID
    query: str | None = None
    nodes: list[DocumentDependencyNode] = []
    edges: list[DocumentDependencyEdge] = []
    links: list[DocumentLinkOut] = []
    total_nodes: int = 0
    total_edges: int = 0


class DocumentDeleteResult(BaseModel):
    document_id: uuid.UUID
    deleted: int = 0
    missing: int = 0
    storage_deleted: int = 0
    details: dict[str, int | str] = {}


class DocumentBulkDeleteRequest(BaseModel):
    document_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)
    delete_files: bool = True


class DocumentBulkDeleteResponse(BaseModel):
    deleted: int = 0
    missing: int = 0
    results: list[DocumentDeleteResult] = []


class DevelopmentPurgeRequest(BaseModel):
    confirm: str
    delete_files: bool = True


class DevelopmentPurgeResponse(BaseModel):
    deleted: int = 0
    missing: int = 0
    documents_seen: int = 0
    results: list[DocumentDeleteResult] = []


class DocumentSummary(BaseModel):
    """Document without relationships — for list and update responses."""

    id: uuid.UUID
    file_name: str
    file_hash: str
    file_size: int
    mime_type: str
    storage_path: str
    page_count: int | None = None
    doc_type: DocumentType | None = None
    doc_type_confidence: float | None = None
    status: DocumentStatus
    source_channel: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DocumentOut(DocumentSummary):
    """Full document with relationships — for get response."""

    extractions: list[DocumentExtractionOut] = []
    links: list[DocumentLinkOut] = []


class DocumentPipelineStatus(BaseModel):
    processing_status: str | None = None
    current_step: str | None = None
    processing_error: str | None = None
    pipeline_steps: list[dict] = []
    extraction_count: int = 0
    artifact_count: int = 0
    graph_status: str | None = None
    graph_scope: str | None = None
    graph_error: str | None = None
    memory_chunks: int = 0
    evidence_spans: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0
    graph_review_pending: int = 0
    embedding_records: int = 0
    ntd_checks: int = 0
    ntd_open_findings: int = 0


class DocumentManagementSummary(BaseModel):
    document: DocumentSummary
    pipeline: DocumentPipelineStatus
    links: list[DocumentLinkOut] = []


class DocumentWorkspaceItem(BaseModel):
    document: DocumentSummary
    pipeline: DocumentPipelineStatus


class DocumentWorkspaceResponse(BaseModel):
    items: list[DocumentWorkspaceItem]
    total: int
    offset: int
    limit: int
    status_counts: dict[str, int] = {}
    doc_type_counts: dict[str, int] = {}


# ── List (doc.list) ─────────────────────────────────────────────────────────


class DocumentListParams(BaseModel):
    status: DocumentStatus | None = None
    doc_type: DocumentType | None = None
    source_channel: str | None = None
    search: str | None = None
    offset: int = 0
    limit: int = Field(50, le=200)


class DocumentListResponse(BaseModel):
    items: list[DocumentSummary]
    total: int
    offset: int
    limit: int


# ── Update (doc.update) ─────────────────────────────────────────────────────


class DocumentUpdate(BaseModel):
    file_name: str | None = None
    doc_type: DocumentType | None = None
    status: DocumentStatus | None = None
    source_channel: str | None = None
    manual_doc_type_override: bool | None = None
    metadata_: dict | None = Field(None, alias="metadata")


class DocumentBatchRequest(BaseModel):
    document_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)
    force: bool = False
    build_scope: str | None = None


class DocumentBatchActionResult(BaseModel):
    document_id: uuid.UUID
    status: str
    task_id: str | None = None
    detail: str | None = None


class DocumentBatchActionResponse(BaseModel):
    action: str
    results: list[DocumentBatchActionResult]


# ── Link (doc.link) ─────────────────────────────────────────────────────────


class DocumentLinkCreate(BaseModel):
    linked_entity_type: str
    linked_entity_id: uuid.UUID
    link_type: str = "related"


# ── Classify / Extract (doc.classify, doc.extract) ─────────────────────────


class TaskResponse(BaseModel):
    """Response for async Celery task trigger."""

    task_id: str
    document_id: uuid.UUID
    status: str = "queued"


class ClassifyResponse(BaseModel):
    document_id: uuid.UUID
    doc_type: str | None
    confidence: float | None
    status: DocumentStatus

    model_config = {"from_attributes": True}


class ExtractionResponse(BaseModel):
    document_id: uuid.UUID
    extraction_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None
    overall_confidence: float | None = None
    line_count: int = 0
    validation_errors: list[dict] = []
    status: str = "completed"


class FieldCorrectionRequest(BaseModel):
    field_name: str
    corrected_value: str


class FieldCorrectionResponse(BaseModel):
    field_name: str
    old_value: str | None
    corrected_value: str
    extraction_id: uuid.UUID


# ── Summarize (doc.summarize) ──────────────────────────────────────────────


class DocumentSummaryAI(BaseModel):
    document_id: uuid.UUID
    summary: str
    key_facts: list[str] = []
    action_required: str | None = None
    urgency: str = "low"
