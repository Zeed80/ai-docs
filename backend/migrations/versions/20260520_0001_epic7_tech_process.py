"""Epic 7: Agent-driven tech process — new tables and column extensions.

Revision ID: 20260520_0001
Revises: 2bc89634b9df
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects import postgresql

revision = "20260520_0001"
down_revision = "2bc89634b9df"
branch_labels = None
depends_on = None


def _col_exists(table: str, col: str) -> bool:
    insp = sa_inspect(op.get_bind())
    return col in {c["name"] for c in insp.get_columns(table)}


def _table_exists(table: str) -> bool:
    return sa_inspect(op.get_bind()).has_table(table)


def _index_exists(index: str, table: str) -> bool:
    insp = sa_inspect(op.get_bind())
    return any(ix["name"] == index for ix in insp.get_indexes(table))


def upgrade() -> None:
    # ── Extend ManufacturingProcessPlan ───────────────────────────────────────
    for col in [
        ("tp_type", sa.Column("tp_type", sa.String(50), nullable=False, server_default="единичный")),
        ("drawing_id", sa.Column("drawing_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("drawings.id"), nullable=True)),
        ("blank_spec_id", sa.Column("blank_spec_id", postgresql.UUID(as_uuid=True), nullable=True)),
        ("normcontrol_status", sa.Column("normcontrol_status", sa.String(30), nullable=False, server_default="not_checked")),
        ("normcontrol_checked_at", sa.Column("normcontrol_checked_at", sa.DateTime(timezone=True), nullable=True)),
        ("normcontrol_checked_by", sa.Column("normcontrol_checked_by", sa.String(100), nullable=True)),
        ("total_norm_minutes", sa.Column("total_norm_minutes", sa.Float(), nullable=True)),
    ]:
        if not _col_exists("manufacturing_process_plans", col[0]):
            op.add_column("manufacturing_process_plans", col[1])

    for idx_name, col_name in [
        ("ix_mfg_process_plans_tp_type", "tp_type"),
        ("ix_mfg_process_plans_drawing_id", "drawing_id"),
        ("ix_mfg_process_plans_normcontrol_status", "normcontrol_status"),
    ]:
        if not _index_exists(idx_name, "manufacturing_process_plans"):
            op.create_index(idx_name, "manufacturing_process_plans", [col_name])

    # ── Extend ManufacturingOperation ─────────────────────────────────────────
    op_cols = [
        ("gost_operation_code", sa.Column("gost_operation_code", sa.String(10), nullable=True)),
        ("department_code", sa.Column("department_code", sa.String(20), nullable=True)),
        ("workplace_code", sa.Column("workplace_code", sa.String(20), nullable=True)),
        ("tooling_list", sa.Column("tooling_list", postgresql.JSON(astext_type=sa.Text()), nullable=True)),
        ("measuring_tools", sa.Column("measuring_tools", postgresql.JSON(astext_type=sa.Text()), nullable=True)),
        ("to_minutes", sa.Column("to_minutes", sa.Float(), nullable=True)),
        ("tv_minutes", sa.Column("tv_minutes", sa.Float(), nullable=True)),
        ("tob_minutes", sa.Column("tob_minutes", sa.Float(), nullable=True)),
        ("totd_minutes", sa.Column("totd_minutes", sa.Float(), nullable=True)),
        ("tsht_minutes", sa.Column("tsht_minutes", sa.Float(), nullable=True)),
        ("tsht_k_minutes", sa.Column("tsht_k_minutes", sa.Float(), nullable=True)),
        ("tpz_minutes", sa.Column("tpz_minutes", sa.Float(), nullable=True)),
    ]
    for col_name, col in op_cols:
        if not _col_exists("manufacturing_operations", col_name):
            op.add_column("manufacturing_operations", col)

    # ── DrawingTPLink ─────────────────────────────────────────────────────────
    if not _table_exists("drawing_tp_links"):
        op.create_table(
            "drawing_tp_links",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
            sa.Column("drawing_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("drawings.id"), nullable=False),
            sa.Column("process_plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manufacturing_process_plans.id"), nullable=False),
            sa.Column("link_type", sa.String(50), nullable=False, server_default="derived_from"),
            sa.Column("surface_mapping", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("created_by", sa.String(100), nullable=False, server_default="sveta"),
        )
        op.create_index("ix_drawing_tp_links_drawing_id", "drawing_tp_links", ["drawing_id"])
        op.create_index("ix_drawing_tp_links_process_plan_id", "drawing_tp_links", ["process_plan_id"])

    # ── SurfaceMachiningSpec ──────────────────────────────────────────────────
    if not _table_exists("surface_machining_specs"):
        op.create_table(
            "surface_machining_specs",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
            sa.Column("process_plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manufacturing_process_plans.id"), nullable=False),
            sa.Column("operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manufacturing_operations.id"), nullable=True),
            sa.Column("drawing_feature_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("drawing_features.id"), nullable=True),
            sa.Column("surface_type", sa.String(60), nullable=False),
            sa.Column("nominal_mm", sa.Float(), nullable=True),
            sa.Column("upper_tol", sa.Float(), nullable=True),
            sa.Column("lower_tol", sa.Float(), nullable=True),
            sa.Column("roughness_ra", sa.Float(), nullable=True),
            sa.Column("fit_system", sa.String(20), nullable=True),
            sa.Column("machining_method", sa.String(60), nullable=False),
            sa.Column("machining_stage", sa.String(30), nullable=False, server_default="finish"),
            sa.Column("assigned_machine_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manufacturing_resources.id"), nullable=True),
            sa.Column("assigned_tool_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manufacturing_resources.id"), nullable=True),
            sa.Column("allowance_mm", sa.Float(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("metadata_", postgresql.JSON(astext_type=sa.Text()), nullable=True, key="metadata"),
        )
        op.create_index("ix_surface_machining_specs_plan_id", "surface_machining_specs", ["process_plan_id"])
        op.create_index("ix_surface_machining_specs_method", "surface_machining_specs", ["machining_method"])
        op.create_index("ix_surface_machining_specs_type", "surface_machining_specs", ["surface_type"])

    # ── BlankSpec ─────────────────────────────────────────────────────────────
    if not _table_exists("blank_specs"):
        op.create_table(
            "blank_specs",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
            sa.Column("process_plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manufacturing_process_plans.id"), nullable=False, unique=True),
            sa.Column("blank_type", sa.String(80), nullable=False),
            sa.Column("material_grade", sa.String(200), nullable=False),
            sa.Column("standard_gost", sa.String(100), nullable=True),
            sa.Column("dimensions", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("mass_blank_kg", sa.Float(), nullable=True),
            sa.Column("mass_part_kg", sa.Float(), nullable=True),
            sa.Column("utilization_factor", sa.Float(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("reasoning", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(100), nullable=False, server_default="sveta"),
        )
        op.create_index("ix_blank_specs_process_plan_id", "blank_specs", ["process_plan_id"])

    # ── GostFormData ──────────────────────────────────────────────────────────
    if not _table_exists("gost_form_data"):
        op.create_table(
            "gost_form_data",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
            sa.Column("process_plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manufacturing_process_plans.id"), nullable=False),
            sa.Column("operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manufacturing_operations.id"), nullable=True),
            sa.Column("form_type", sa.String(10), nullable=False),
            sa.Column("gost_code", sa.String(50), nullable=False),
            sa.Column("form_variant", sa.String(20), nullable=False, server_default="form1"),
            sa.Column("rendered_data", postgresql.JSON(astext_type=sa.Text()), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("approved_by", sa.String(100), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_gost_form_data_plan_id", "gost_form_data", ["process_plan_id"])
        op.create_index("ix_gost_form_data_form_type", "gost_form_data", ["form_type"])
        op.create_index("ix_gost_form_data_status", "gost_form_data", ["status"])

    # ── NormControlCheck ──────────────────────────────────────────────────────
    if not _table_exists("normcontrol_checks"):
        op.create_table(
            "normcontrol_checks",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
            sa.Column("process_plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manufacturing_process_plans.id"), nullable=False),
            sa.Column("operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manufacturing_operations.id"), nullable=True),
            sa.Column("form_type", sa.String(10), nullable=True),
            sa.Column("gost_code", sa.String(50), nullable=False),
            sa.Column("clause", sa.String(50), nullable=True),
            sa.Column("check_code", sa.String(80), nullable=False),
            sa.Column("severity", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("recommendation", sa.Text(), nullable=True),
            sa.Column("auto_fixable", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("evidence", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("created_by", sa.String(100), nullable=False, server_default="normcontrol_agent"),
            sa.Column("resolved_by", sa.String(100), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_normcontrol_checks_plan_id", "normcontrol_checks", ["process_plan_id"])
        op.create_index("ix_normcontrol_checks_severity", "normcontrol_checks", ["severity"])
        op.create_index("ix_normcontrol_checks_status", "normcontrol_checks", ["status"])
        op.create_index("ix_normcontrol_checks_gost_code", "normcontrol_checks", ["gost_code"])
        op.create_index("ix_normcontrol_checks_check_code", "normcontrol_checks", ["check_code"])


def downgrade() -> None:
    op.drop_table("normcontrol_checks")
    op.drop_table("gost_form_data")
    op.drop_table("blank_specs")
    op.drop_table("surface_machining_specs")
    op.drop_table("drawing_tp_links")

    for col in [
        "tpz_minutes", "tsht_k_minutes", "tsht_minutes", "totd_minutes",
        "tob_minutes", "tv_minutes", "to_minutes", "measuring_tools",
        "tooling_list", "workplace_code", "department_code", "gost_operation_code",
    ]:
        op.drop_column("manufacturing_operations", col)

    op.drop_index("ix_mfg_process_plans_normcontrol_status", "manufacturing_process_plans")
    op.drop_index("ix_mfg_process_plans_drawing_id", "manufacturing_process_plans")
    op.drop_index("ix_mfg_process_plans_tp_type", "manufacturing_process_plans")
    for col in [
        "total_norm_minutes", "normcontrol_checked_by", "normcontrol_checked_at",
        "normcontrol_status", "blank_spec_id", "drawing_id", "tp_type",
    ]:
        op.drop_column("manufacturing_process_plans", col)
