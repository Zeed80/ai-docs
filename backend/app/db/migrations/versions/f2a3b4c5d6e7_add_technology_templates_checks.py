"""add technology templates and checks

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-04-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "manufacturing_operation_templates",
        sa.Column("operation_type", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("standard_system", sa.String(length=100), nullable=False),
        sa.Column("default_operation_code", sa.String(length=50), nullable=True),
        sa.Column("required_resource_types", sa.JSON(), nullable=True),
        sa.Column("default_transition_text", sa.Text(), nullable=True),
        sa.Column("default_control_requirements", sa.Text(), nullable=True),
        sa.Column("default_safety_requirements", sa.Text(), nullable=True),
        sa.Column("parameters_schema", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_manufacturing_operation_templates_operation_type"), "manufacturing_operation_templates", ["operation_type"])
    op.create_index(op.f("ix_manufacturing_operation_templates_name"), "manufacturing_operation_templates", ["name"])

    op.create_table(
        "manufacturing_check_results",
        sa.Column("process_plan_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("check_code", sa.String(length=100), nullable=False),
        sa.Column("severity", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("resolved_by", sa.String(length=100), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["operation_id"], ["manufacturing_operations.id"]),
        sa.ForeignKeyConstraint(["process_plan_id"], ["manufacturing_process_plans.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_manufacturing_check_results_process_plan_id"), "manufacturing_check_results", ["process_plan_id"])
    op.create_index(op.f("ix_manufacturing_check_results_operation_id"), "manufacturing_check_results", ["operation_id"])
    op.create_index(op.f("ix_manufacturing_check_results_check_code"), "manufacturing_check_results", ["check_code"])
    op.create_index(op.f("ix_manufacturing_check_results_severity"), "manufacturing_check_results", ["severity"])
    op.create_index(op.f("ix_manufacturing_check_results_status"), "manufacturing_check_results", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_manufacturing_check_results_status"), table_name="manufacturing_check_results")
    op.drop_index(op.f("ix_manufacturing_check_results_severity"), table_name="manufacturing_check_results")
    op.drop_index(op.f("ix_manufacturing_check_results_check_code"), table_name="manufacturing_check_results")
    op.drop_index(op.f("ix_manufacturing_check_results_operation_id"), table_name="manufacturing_check_results")
    op.drop_index(op.f("ix_manufacturing_check_results_process_plan_id"), table_name="manufacturing_check_results")
    op.drop_table("manufacturing_check_results")
    op.drop_index(op.f("ix_manufacturing_operation_templates_name"), table_name="manufacturing_operation_templates")
    op.drop_index(op.f("ix_manufacturing_operation_templates_operation_type"), table_name="manufacturing_operation_templates")
    op.drop_table("manufacturing_operation_templates")
