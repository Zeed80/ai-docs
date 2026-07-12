"""Engineering release validation runs.

Revision ID: 20260712_0004
Revises: 20260712_0003
Create Date: 2026-07-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from app.db.base import GUID

revision = "20260712_0004"
down_revision = "20260712_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "engineering_validation_runs" in set(sa_inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "engineering_validation_runs",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("engineering_revision_id", GUID(), sa.ForeignKey("engineering_revisions.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="passed"),
        sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("findings", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("initiated_by", sa.String(255)),
    )
    op.create_index("ix_engineering_validation_runs_engineering_revision_id", "engineering_validation_runs", ["engineering_revision_id"])
    op.create_index("ix_engineering_validation_runs_status", "engineering_validation_runs", ["status"])


def downgrade() -> None:
    op.drop_table("engineering_validation_runs")
