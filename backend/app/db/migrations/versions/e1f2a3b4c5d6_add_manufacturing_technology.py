"""add manufacturing technology tables

Revision ID: e1f2a3b4c5d6
Revises: d9a1b6e4c230
Create Date: 2026-04-27 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d9a1b6e4c230"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "manufacturing_resources",
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("standard", sa.String(length=200), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_manufacturing_resources_resource_type"), "manufacturing_resources", ["resource_type"])
    op.create_index(op.f("ix_manufacturing_resources_name"), "manufacturing_resources", ["name"])
    op.create_index(op.f("ix_manufacturing_resources_code"), "manufacturing_resources", ["code"])

    op.create_table(
        "manufacturing_process_plans",
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("bom_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("product_name", sa.String(length=500), nullable=False),
        sa.Column("product_code", sa.String(length=100), nullable=True),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("standard_system", sa.String(length=100), nullable=False),
        sa.Column("route_summary", sa.Text(), nullable=True),
        sa.Column("material", sa.String(length=300), nullable=True),
        sa.Column("blank_type", sa.String(length=300), nullable=True),
        sa.Column("quality_requirements", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["bom_id"], ["boms.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_manufacturing_process_plans_document_id"), "manufacturing_process_plans", ["document_id"])
    op.create_index(op.f("ix_manufacturing_process_plans_bom_id"), "manufacturing_process_plans", ["bom_id"])
    op.create_index(op.f("ix_manufacturing_process_plans_product_name"), "manufacturing_process_plans", ["product_name"])
    op.create_index(op.f("ix_manufacturing_process_plans_product_code"), "manufacturing_process_plans", ["product_code"])
    op.create_index(op.f("ix_manufacturing_process_plans_status"), "manufacturing_process_plans", ["status"])

    op.create_table(
        "manufacturing_operations",
        sa.Column("process_plan_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("operation_code", sa.String(length=50), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("operation_type", sa.String(length=100), nullable=True),
        sa.Column("machine_resource_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("tool_resource_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("fixture_resource_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("setup_description", sa.Text(), nullable=True),
        sa.Column("transition_text", sa.Text(), nullable=True),
        sa.Column("cutting_parameters", sa.JSON(), nullable=True),
        sa.Column("control_requirements", sa.Text(), nullable=True),
        sa.Column("safety_requirements", sa.Text(), nullable=True),
        sa.Column("setup_minutes", sa.Float(), nullable=True),
        sa.Column("machine_minutes", sa.Float(), nullable=True),
        sa.Column("labor_minutes", sa.Float(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["fixture_resource_id"], ["manufacturing_resources.id"]),
        sa.ForeignKeyConstraint(["machine_resource_id"], ["manufacturing_resources.id"]),
        sa.ForeignKeyConstraint(["process_plan_id"], ["manufacturing_process_plans.id"]),
        sa.ForeignKeyConstraint(["tool_resource_id"], ["manufacturing_resources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_manufacturing_operations_process_plan_id"), "manufacturing_operations", ["process_plan_id"])
    op.create_index(op.f("ix_manufacturing_operations_name"), "manufacturing_operations", ["name"])
    op.create_index(op.f("ix_manufacturing_operations_operation_type"), "manufacturing_operations", ["operation_type"])
    op.create_index(op.f("ix_manufacturing_operations_machine_resource_id"), "manufacturing_operations", ["machine_resource_id"])
    op.create_index(op.f("ix_manufacturing_operations_tool_resource_id"), "manufacturing_operations", ["tool_resource_id"])
    op.create_index(op.f("ix_manufacturing_operations_fixture_resource_id"), "manufacturing_operations", ["fixture_resource_id"])

    op.create_table(
        "manufacturing_norm_estimates",
        sa.Column("process_plan_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("setup_minutes", sa.Float(), nullable=True),
        sa.Column("machine_minutes", sa.Float(), nullable=True),
        sa.Column("labor_minutes", sa.Float(), nullable=True),
        sa.Column("batch_size", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("method", sa.String(length=100), nullable=False),
        sa.Column("assumptions", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["operation_id"], ["manufacturing_operations.id"]),
        sa.ForeignKeyConstraint(["process_plan_id"], ["manufacturing_process_plans.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_manufacturing_norm_estimates_process_plan_id"), "manufacturing_norm_estimates", ["process_plan_id"])
    op.create_index(op.f("ix_manufacturing_norm_estimates_operation_id"), "manufacturing_norm_estimates", ["operation_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_manufacturing_norm_estimates_operation_id"), table_name="manufacturing_norm_estimates")
    op.drop_index(op.f("ix_manufacturing_norm_estimates_process_plan_id"), table_name="manufacturing_norm_estimates")
    op.drop_table("manufacturing_norm_estimates")
    op.drop_index(op.f("ix_manufacturing_operations_fixture_resource_id"), table_name="manufacturing_operations")
    op.drop_index(op.f("ix_manufacturing_operations_tool_resource_id"), table_name="manufacturing_operations")
    op.drop_index(op.f("ix_manufacturing_operations_machine_resource_id"), table_name="manufacturing_operations")
    op.drop_index(op.f("ix_manufacturing_operations_operation_type"), table_name="manufacturing_operations")
    op.drop_index(op.f("ix_manufacturing_operations_name"), table_name="manufacturing_operations")
    op.drop_index(op.f("ix_manufacturing_operations_process_plan_id"), table_name="manufacturing_operations")
    op.drop_table("manufacturing_operations")
    op.drop_index(op.f("ix_manufacturing_process_plans_status"), table_name="manufacturing_process_plans")
    op.drop_index(op.f("ix_manufacturing_process_plans_product_code"), table_name="manufacturing_process_plans")
    op.drop_index(op.f("ix_manufacturing_process_plans_product_name"), table_name="manufacturing_process_plans")
    op.drop_index(op.f("ix_manufacturing_process_plans_bom_id"), table_name="manufacturing_process_plans")
    op.drop_index(op.f("ix_manufacturing_process_plans_document_id"), table_name="manufacturing_process_plans")
    op.drop_table("manufacturing_process_plans")
    op.drop_index(op.f("ix_manufacturing_resources_code"), table_name="manufacturing_resources")
    op.drop_index(op.f("ix_manufacturing_resources_name"), table_name="manufacturing_resources")
    op.drop_index(op.f("ix_manufacturing_resources_resource_type"), table_name="manufacturing_resources")
    op.drop_table("manufacturing_resources")
