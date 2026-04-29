"""add technology corrections

Revision ID: 0a1b2c3d4e5f
Revises: f2a3b4c5d6e7
Create Date: 2026-04-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "0a1b2c3d4e5f"
down_revision: Union[str, None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "technology_corrections",
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("correction_type", sa.String(length=80), nullable=False),
        sa.Column("corrected_by", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source_document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("process_plan_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("operation_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["operation_id"], ["manufacturing_operations.id"]),
        sa.ForeignKeyConstraint(["process_plan_id"], ["manufacturing_process_plans.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_technology_corrections_entity_type"), "technology_corrections", ["entity_type"])
    op.create_index(op.f("ix_technology_corrections_entity_id"), "technology_corrections", ["entity_id"])
    op.create_index(op.f("ix_technology_corrections_field_name"), "technology_corrections", ["field_name"])
    op.create_index(op.f("ix_technology_corrections_source_document_id"), "technology_corrections", ["source_document_id"])
    op.create_index(op.f("ix_technology_corrections_process_plan_id"), "technology_corrections", ["process_plan_id"])
    op.create_index(op.f("ix_technology_corrections_operation_id"), "technology_corrections", ["operation_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_technology_corrections_operation_id"), table_name="technology_corrections")
    op.drop_index(op.f("ix_technology_corrections_process_plan_id"), table_name="technology_corrections")
    op.drop_index(op.f("ix_technology_corrections_source_document_id"), table_name="technology_corrections")
    op.drop_index(op.f("ix_technology_corrections_field_name"), table_name="technology_corrections")
    op.drop_index(op.f("ix_technology_corrections_entity_id"), table_name="technology_corrections")
    op.drop_index(op.f("ix_technology_corrections_entity_type"), table_name="technology_corrections")
    op.drop_table("technology_corrections")
