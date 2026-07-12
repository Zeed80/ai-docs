"""Engineering calculation cases.

Revision ID: 20260712_0005
Revises: 20260712_0004
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260712_0005"
down_revision = "20260712_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "engineering_analysis_cases" in set(sa_inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "engineering_analysis_cases",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("engineering_revision_id", GUID(), sa.ForeignKey("engineering_revisions.id"), nullable=False),
        sa.Column("material_id", GUID(), sa.ForeignKey("engineering_materials.id")),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("analysis_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("inputs", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("results", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("assumptions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("solver", sa.String(80), nullable=False, server_default="analytical"),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_engineering_analysis_cases_engineering_revision_id", "engineering_analysis_cases", ["engineering_revision_id"])
    op.create_index("ix_engineering_analysis_cases_material_id", "engineering_analysis_cases", ["material_id"])
    op.create_index("ix_engineering_analysis_cases_status", "engineering_analysis_cases", ["status"])
def downgrade() -> None:
    op.drop_table("engineering_analysis_cases")
