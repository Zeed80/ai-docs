"""F2: immutable analysis run snapshots.

Revision ID: 20260716_0003
Revises: 20260716_0002
Create Date: 2026-07-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260716_0003"
down_revision = "20260716_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "engineering_analysis_runs" in set(sa_inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "engineering_analysis_runs",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("analysis_case_id", GUID(), sa.ForeignKey("engineering_analysis_cases.id"), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("inputs_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("material_snapshot", sa.JSON()),
        sa.Column("solver_name", sa.String(80), nullable=False),
        sa.Column("solver_version", sa.String(40), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("assumptions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text()),
        sa.Column("executed_by", sa.String(255)),
    )
    op.create_index("ix_engineering_analysis_runs_analysis_case_id", "engineering_analysis_runs", ["analysis_case_id"])
    op.create_index("ix_engineering_analysis_runs_status", "engineering_analysis_runs", ["status"])
    op.create_index("ix_engineering_analysis_runs_case_number", "engineering_analysis_runs", ["analysis_case_id", "run_number"], unique=True)


def downgrade() -> None:
    op.drop_table("engineering_analysis_runs")
