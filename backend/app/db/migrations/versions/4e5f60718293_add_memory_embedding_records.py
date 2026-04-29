"""add memory embedding records

Revision ID: 4e5f60718293
Revises: 3d4e5f607182
Create Date: 2026-04-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "4e5f60718293"
down_revision: Union[str, None] = "3d4e5f607182"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "memory_embedding_records",
        sa.Column("content_type", sa.String(length=50), nullable=False),
        sa.Column("content_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("document_version_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("collection_name", sa.String(length=100), nullable=False),
        sa.Column("point_id", sa.String(length=200), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("vector_size", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_memory_embedding_records_content_type"), "memory_embedding_records", ["content_type"])
    op.create_index(op.f("ix_memory_embedding_records_content_id"), "memory_embedding_records", ["content_id"])
    op.create_index(op.f("ix_memory_embedding_records_document_id"), "memory_embedding_records", ["document_id"])
    op.create_index(op.f("ix_memory_embedding_records_document_version_id"), "memory_embedding_records", ["document_version_id"])
    op.create_index(op.f("ix_memory_embedding_records_point_id"), "memory_embedding_records", ["point_id"])
    op.create_index(op.f("ix_memory_embedding_records_status"), "memory_embedding_records", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_memory_embedding_records_status"), table_name="memory_embedding_records")
    op.drop_index(op.f("ix_memory_embedding_records_point_id"), table_name="memory_embedding_records")
    op.drop_index(op.f("ix_memory_embedding_records_document_version_id"), table_name="memory_embedding_records")
    op.drop_index(op.f("ix_memory_embedding_records_document_id"), table_name="memory_embedding_records")
    op.drop_index(op.f("ix_memory_embedding_records_content_id"), table_name="memory_embedding_records")
    op.drop_index(op.f("ix_memory_embedding_records_content_type"), table_name="memory_embedding_records")
    op.drop_table("memory_embedding_records")
