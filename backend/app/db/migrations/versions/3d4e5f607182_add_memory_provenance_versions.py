"""add memory provenance versions

Revision ID: 3d4e5f607182
Revises: 2c3d4e5f6071
Create Date: 2026-04-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "3d4e5f607182"
down_revision: Union[str, None] = "2c3d4e5f6071"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("knowledge_nodes", sa.Column("source_document_id", PG_UUID(as_uuid=True), nullable=True))
    op.add_column("knowledge_nodes", sa.Column("source_document_version_id", PG_UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(None, "knowledge_nodes", "documents", ["source_document_id"], ["id"])
    op.create_foreign_key(None, "knowledge_nodes", "document_versions", ["source_document_version_id"], ["id"])
    op.create_index(op.f("ix_knowledge_nodes_source_document_id"), "knowledge_nodes", ["source_document_id"])
    op.create_index(op.f("ix_knowledge_nodes_source_document_version_id"), "knowledge_nodes", ["source_document_version_id"])

    op.add_column("knowledge_edges", sa.Column("source_document_version_id", PG_UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(None, "knowledge_edges", "document_versions", ["source_document_version_id"], ["id"])
    op.create_index(op.f("ix_knowledge_edges_source_document_version_id"), "knowledge_edges", ["source_document_version_id"])

    op.add_column("document_chunks", sa.Column("document_version_id", PG_UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(None, "document_chunks", "document_versions", ["document_version_id"], ["id"])
    op.create_index(op.f("ix_document_chunks_document_version_id"), "document_chunks", ["document_version_id"])

    op.add_column("evidence_spans", sa.Column("document_version_id", PG_UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(None, "evidence_spans", "document_versions", ["document_version_id"], ["id"])
    op.create_index(op.f("ix_evidence_spans_document_version_id"), "evidence_spans", ["document_version_id"])

    op.add_column("entity_mentions", sa.Column("document_version_id", PG_UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(None, "entity_mentions", "document_versions", ["document_version_id"], ["id"])
    op.create_index(op.f("ix_entity_mentions_document_version_id"), "entity_mentions", ["document_version_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_entity_mentions_document_version_id"), table_name="entity_mentions")
    op.drop_column("entity_mentions", "document_version_id")
    op.drop_index(op.f("ix_evidence_spans_document_version_id"), table_name="evidence_spans")
    op.drop_column("evidence_spans", "document_version_id")
    op.drop_index(op.f("ix_document_chunks_document_version_id"), table_name="document_chunks")
    op.drop_column("document_chunks", "document_version_id")
    op.drop_index(op.f("ix_knowledge_edges_source_document_version_id"), table_name="knowledge_edges")
    op.drop_column("knowledge_edges", "source_document_version_id")
    op.drop_index(op.f("ix_knowledge_nodes_source_document_version_id"), table_name="knowledge_nodes")
    op.drop_index(op.f("ix_knowledge_nodes_source_document_id"), table_name="knowledge_nodes")
    op.drop_column("knowledge_nodes", "source_document_version_id")
    op.drop_column("knowledge_nodes", "source_document_id")
