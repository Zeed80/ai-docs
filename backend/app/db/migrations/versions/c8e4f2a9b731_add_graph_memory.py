"""add graph memory tables

Revision ID: c8e4f2a9b731
Revises: 5c09365341d1
Create Date: 2026-04-27 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "c8e4f2a9b731"
down_revision: Union[str, None] = "5c09365341d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_nodes",
        sa.Column("node_type", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("canonical_key", sa.String(length=500), nullable=True),
        sa.Column("entity_type", sa.String(length=80), nullable=True),
        sa.Column("entity_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("aliases", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_by", sa.String(length=50), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_knowledge_nodes_node_type"), "knowledge_nodes", ["node_type"])
    op.create_index(op.f("ix_knowledge_nodes_title"), "knowledge_nodes", ["title"])
    op.create_index(op.f("ix_knowledge_nodes_canonical_key"), "knowledge_nodes", ["canonical_key"])
    op.create_index(op.f("ix_knowledge_nodes_entity_type"), "knowledge_nodes", ["entity_type"])
    op.create_index(op.f("ix_knowledge_nodes_entity_id"), "knowledge_nodes", ["entity_id"])

    op.create_table(
        "document_chunks",
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("bbox_data", sa.JSON(), nullable=True),
        sa.Column("embedding_id", sa.String(length=200), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_document_chunks_document_id"), "document_chunks", ["document_id"])
    op.create_index(op.f("ix_document_chunks_embedding_id"), "document_chunks", ["embedding_id"])

    op.create_table(
        "evidence_spans",
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("bbox_data", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_evidence_spans_document_id"), "evidence_spans", ["document_id"])
    op.create_index(op.f("ix_evidence_spans_chunk_id"), "evidence_spans", ["chunk_id"])
    op.create_index(op.f("ix_evidence_spans_field_name"), "evidence_spans", ["field_name"])

    op.create_table(
        "knowledge_edges",
        sa.Column("source_node_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("target_node_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("edge_type", sa.String(length=80), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source_document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_span_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", sa.String(length=50), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["evidence_span_id"], ["evidence_spans.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["source_node_id"], ["knowledge_nodes.id"]),
        sa.ForeignKeyConstraint(["target_node_id"], ["knowledge_nodes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_knowledge_edges_source_node_id"), "knowledge_edges", ["source_node_id"])
    op.create_index(op.f("ix_knowledge_edges_target_node_id"), "knowledge_edges", ["target_node_id"])
    op.create_index(op.f("ix_knowledge_edges_edge_type"), "knowledge_edges", ["edge_type"])
    op.create_index(op.f("ix_knowledge_edges_source_document_id"), "knowledge_edges", ["source_document_id"])
    op.create_index(op.f("ix_knowledge_edges_evidence_span_id"), "knowledge_edges", ["evidence_span_id"])

    op.create_table(
        "entity_mentions",
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("node_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("mention_text", sa.String(length=500), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("extraction_method", sa.String(length=80), nullable=False),
        sa.Column("evidence_span_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["evidence_span_id"], ["evidence_spans.id"]),
        sa.ForeignKeyConstraint(["node_id"], ["knowledge_nodes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_entity_mentions_document_id"), "entity_mentions", ["document_id"])
    op.create_index(op.f("ix_entity_mentions_chunk_id"), "entity_mentions", ["chunk_id"])
    op.create_index(op.f("ix_entity_mentions_node_id"), "entity_mentions", ["node_id"])
    op.create_index(op.f("ix_entity_mentions_mention_text"), "entity_mentions", ["mention_text"])
    op.create_index(op.f("ix_entity_mentions_entity_type"), "entity_mentions", ["entity_type"])
    op.create_index(op.f("ix_entity_mentions_evidence_span_id"), "entity_mentions", ["evidence_span_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_entity_mentions_evidence_span_id"), table_name="entity_mentions")
    op.drop_index(op.f("ix_entity_mentions_entity_type"), table_name="entity_mentions")
    op.drop_index(op.f("ix_entity_mentions_mention_text"), table_name="entity_mentions")
    op.drop_index(op.f("ix_entity_mentions_node_id"), table_name="entity_mentions")
    op.drop_index(op.f("ix_entity_mentions_chunk_id"), table_name="entity_mentions")
    op.drop_index(op.f("ix_entity_mentions_document_id"), table_name="entity_mentions")
    op.drop_table("entity_mentions")
    op.drop_index(op.f("ix_knowledge_edges_evidence_span_id"), table_name="knowledge_edges")
    op.drop_index(op.f("ix_knowledge_edges_source_document_id"), table_name="knowledge_edges")
    op.drop_index(op.f("ix_knowledge_edges_edge_type"), table_name="knowledge_edges")
    op.drop_index(op.f("ix_knowledge_edges_target_node_id"), table_name="knowledge_edges")
    op.drop_index(op.f("ix_knowledge_edges_source_node_id"), table_name="knowledge_edges")
    op.drop_table("knowledge_edges")
    op.drop_index(op.f("ix_evidence_spans_field_name"), table_name="evidence_spans")
    op.drop_index(op.f("ix_evidence_spans_chunk_id"), table_name="evidence_spans")
    op.drop_index(op.f("ix_evidence_spans_document_id"), table_name="evidence_spans")
    op.drop_table("evidence_spans")
    op.drop_index(op.f("ix_document_chunks_embedding_id"), table_name="document_chunks")
    op.drop_index(op.f("ix_document_chunks_document_id"), table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index(op.f("ix_knowledge_nodes_entity_id"), table_name="knowledge_nodes")
    op.drop_index(op.f("ix_knowledge_nodes_entity_type"), table_name="knowledge_nodes")
    op.drop_index(op.f("ix_knowledge_nodes_canonical_key"), table_name="knowledge_nodes")
    op.drop_index(op.f("ix_knowledge_nodes_title"), table_name="knowledge_nodes")
    op.drop_index(op.f("ix_knowledge_nodes_node_type"), table_name="knowledge_nodes")
    op.drop_table("knowledge_nodes")
