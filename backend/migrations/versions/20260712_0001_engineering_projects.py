"""Canonical engineering projects, immutable revisions and projections.

Revision ID: 20260712_0001
Revises: 20260711_0001
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from app.db.base import GUID

revision = "20260712_0001"
down_revision = "20260711_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())
    tables = set(insp.get_table_names())
    if "engineering_projects" not in tables:
        op.create_table(
        "engineering_projects",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("project_id", GUID(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("code", sa.String(100), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("code", name="uq_engineering_projects_code"),
        )
        op.create_index("ix_engineering_projects_project_id", "engineering_projects", ["project_id"])
        op.create_index("ix_engineering_projects_name", "engineering_projects", ["name"])
        op.create_index("ix_engineering_projects_code", "engineering_projects", ["code"])
        op.create_index("ix_engineering_projects_status", "engineering_projects", ["status"])
    if "engineering_revisions" not in tables:
        op.create_table(
        "engineering_revisions",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("engineering_project_id", GUID(), sa.ForeignKey("engineering_projects.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("origin", sa.String(30), nullable=False, server_default="manual"),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("validation", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_engineering_revisions_engineering_project_id", "engineering_revisions", ["engineering_project_id"])
        op.create_index("ix_engineering_revisions_status", "engineering_revisions", ["status"])
        op.create_index("ix_engineering_revisions_project_revision", "engineering_revisions", ["engineering_project_id", "revision"], unique=True)
    if "engineering_projections" not in tables:
        op.create_table(
        "engineering_projections",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("engineering_revision_id", GUID(), sa.ForeignKey("engineering_revisions.id"), nullable=False),
        sa.Column("projection_type", sa.String(40), nullable=False),
        sa.Column("entity_type", sa.String(80), nullable=False),
        sa.Column("entity_id", GUID(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False, server_default="current"),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("engineering_revision_id", "projection_type", "entity_type", "entity_id", name="uq_engineering_projection_target"),
        )
        op.create_index("ix_engineering_projections_engineering_revision_id", "engineering_projections", ["engineering_revision_id"])
        op.create_index("ix_engineering_projections_projection_type", "engineering_projections", ["projection_type"])
        op.create_index("ix_engineering_projections_entity_type", "engineering_projections", ["entity_type"])
        op.create_index("ix_engineering_projections_entity_id", "engineering_projections", ["entity_id"])
        op.create_index("ix_engineering_projections_state", "engineering_projections", ["state"])
        op.create_index("ix_engineering_projection_target", "engineering_projections", ["entity_type", "entity_id"])
    for table, index in (
        ("drawings", "ix_drawings_engineering_revision_id"),
        ("boms", "ix_boms_engineering_revision_id"),
        ("manufacturing_process_plans", "ix_manufacturing_process_plans_engineering_revision_id"),
    ):
        columns = {column["name"] for column in sa_inspect(op.get_bind()).get_columns(table)}
        if "engineering_revision_id" not in columns:
            op.add_column(table, sa.Column("engineering_revision_id", GUID(), sa.ForeignKey("engineering_revisions.id"), nullable=True))
            op.create_index(index, table, ["engineering_revision_id"])


def downgrade() -> None:
    op.drop_index("ix_manufacturing_process_plans_engineering_revision_id", table_name="manufacturing_process_plans")
    op.drop_column("manufacturing_process_plans", "engineering_revision_id")
    op.drop_index("ix_boms_engineering_revision_id", table_name="boms")
    op.drop_column("boms", "engineering_revision_id")
    op.drop_index("ix_drawings_engineering_revision_id", table_name="drawings")
    op.drop_column("drawings", "engineering_revision_id")
    op.drop_table("engineering_projections")
    op.drop_table("engineering_revisions")
    op.drop_table("engineering_projects")
