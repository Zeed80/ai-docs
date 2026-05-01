"""Pydantic schemas for graph memory."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field


class KnowledgeNodeCreate(BaseModel):
    node_type: str = Field(..., min_length=1, max_length=80)
    title: str = Field(..., min_length=1, max_length=500)
    canonical_key: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    summary: str | None = None
    aliases: list[str] | None = None
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class KnowledgeNodeOut(BaseModel):
    id: uuid.UUID
    node_type: str
    title: str
    canonical_key: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    summary: str | None = None
    aliases: list | None = None
    confidence: float
    created_by: str
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    source_document_id: uuid.UUID | None = None
    source_document_version_id: uuid.UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class KnowledgeEdgeCreate(BaseModel):
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    edge_type: str = Field(..., min_length=1, max_length=80)
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    reason: str | None = None
    source_document_id: uuid.UUID | None = None
    source_document_version_id: uuid.UUID | None = None
    evidence_span_id: uuid.UUID | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class KnowledgeEdgeOut(BaseModel):
    id: uuid.UUID
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    edge_type: str
    confidence: float
    reason: str | None = None
    source_document_id: uuid.UUID | None = None
    source_document_version_id: uuid.UUID | None = None
    evidence_span_id: uuid.UUID | None = None
    created_by: str
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class DocumentChunkCreate(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    chunk_index: int = Field(..., ge=0)
    text: str = Field(..., min_length=1)
    token_count: int | None = None
    page_number: int | None = None
    bbox_data: dict | None = None
    embedding_id: str | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class DocumentChunkOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    chunk_index: int
    text: str
    token_count: int | None = None
    page_number: int | None = None
    bbox_data: dict | None = None
    embedding_id: str | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class EvidenceSpanCreate(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    chunk_id: uuid.UUID | None = None
    field_name: str | None = None
    text: str = Field(..., min_length=1)
    page_number: int | None = None
    bbox_data: dict | None = None
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class EvidenceSpanOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    chunk_id: uuid.UUID | None = None
    field_name: str | None = None
    text: str
    page_number: int | None = None
    bbox_data: dict | None = None
    confidence: float
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class EntityMentionCreate(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    chunk_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None
    mention_text: str = Field(..., min_length=1, max_length=500)
    entity_type: str = Field(..., min_length=1, max_length=80)
    start_offset: int | None = None
    end_offset: int | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    extraction_method: str = "manual"
    evidence_span_id: uuid.UUID | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class EntityMentionOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    chunk_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None
    mention_text: str
    entity_type: str
    start_offset: int | None = None
    end_offset: int | None = None
    confidence: float
    extraction_method: str
    evidence_span_id: uuid.UUID | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class GraphNeighborhoodResponse(BaseModel):
    center: KnowledgeNodeOut
    nodes: list[KnowledgeNodeOut]
    edges: list[KnowledgeEdgeOut]


class GraphPathResponse(BaseModel):
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    found: bool
    nodes: list[KnowledgeNodeOut] = []
    edges: list[KnowledgeEdgeOut] = []


class GraphReviewItemOut(BaseModel):
    id: uuid.UUID
    item_type: str
    status: str
    document_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None
    edge_id: uuid.UUID | None = None
    mention_id: uuid.UUID | None = None
    confidence: float
    reason: str | None = None
    suggested_by: str
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_comment: str | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class GraphReviewListResponse(BaseModel):
    items: list[GraphReviewItemOut]
    total: int


class GraphReviewDecision(BaseModel):
    action: str = Field(..., pattern="^(approve|reject)$")
    decided_by: str = Field("system", min_length=1, max_length=100)
    comment: str | None = None


class MemorySearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    node_types: list[str] | None = None
    document_id: uuid.UUID | None = None
    limit: int = Field(20, ge=1, le=100)
    retrieval_mode: Literal["sql", "sql_vector", "sql_vector_rerank", "graph", "hybrid"] = "sql"
    include_explain: bool = True


class MemorySearchHit(BaseModel):
    kind: str
    id: uuid.UUID
    title: str
    summary: str | None = None
    score: float
    source: str = "sql"
    text_score: float | None = None
    vector_score: float | None = None
    graph_score: float | None = None
    rerank_score: float | None = None
    source_document_id: uuid.UUID | None = None
    evidence: EvidenceSpanOut | None = None


class MemorySearchResponse(BaseModel):
    query: str
    retrieval_mode: str = "sql"
    hits: list[MemorySearchHit]
    total: int


class MemoryExplainRequest(BaseModel):
    query: str = Field(..., min_length=1)
    document_id: uuid.UUID | None = None
    node_types: list[str] | None = None
    limit: int = Field(10, ge=1, le=50)
    neighborhood_depth: int = Field(1, ge=0, le=2)


class MemoryExplainResponse(BaseModel):
    query: str
    hits: list[MemorySearchHit]
    nodes: list[KnowledgeNodeOut]
    edges: list[KnowledgeEdgeOut]
    evidence: list[EvidenceSpanOut]
    total_hits: int


class MemoryEmbeddingRebuildRequest(BaseModel):
    document_id: uuid.UUID | None = None
    content_types: list[str] = ["document_chunk", "evidence_span"]
    collection_name: str = "memory_chunks"
    embedding_model: str = "nomic-embed-text"
    vector_size: int | None = None
    limit: int = Field(500, ge=1, le=5000)
    mark_stale_existing: bool = True


class MemoryEmbeddingRecordOut(BaseModel):
    id: uuid.UUID
    content_type: str
    content_id: uuid.UUID
    document_id: uuid.UUID | None = None
    document_version_id: uuid.UUID | None = None
    collection_name: str
    point_id: str
    embedding_model: str
    vector_size: int | None = None
    status: str
    error: str | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class MemoryEmbeddingRebuildResponse(BaseModel):
    records: list[MemoryEmbeddingRecordOut]
    created: int
    stale_marked: int


class MemoryEmbeddingStatsResponse(BaseModel):
    active_model: str
    active_collection: str
    dimension: int
    counts_by_status: dict[str, int]
    total: int


class MemoryEmbeddingIndexRequest(BaseModel):
    document_id: uuid.UUID | None = None
    statuses: list[str] = ["queued", "stale"]
    limit: int = Field(100, ge=1, le=1000)


class MemoryEmbeddingIndexResponse(BaseModel):
    indexed: int
    failed: int
    skipped: int
    collection_name: str
    embedding_model: str


class MemoryReindexRequest(BaseModel):
    document_ids: list[uuid.UUID] | None = None
    rebuild: bool = True
    limit: int = Field(100, ge=1, le=1000)


class MemoryReindexItem(BaseModel):
    document_id: uuid.UUID
    document_node_id: uuid.UUID
    chunks_created: int
    evidence_created: int
    mentions_created: int
    edges_created: int
    review_items_created: int


class MemoryReindexResponse(BaseModel):
    processed: int
    items: list[MemoryReindexItem]
