"""E4: EBOM/MBOM distinction and richer BOM lines.

Revision ID: 20260716_0002
Revises: 20260716_0001
Create Date: 2026-07-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260716_0002"
down_revision = "20260716_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa_inspect(op.get_bind())
    bom_columns = {column["name"] for column in inspector.get_columns("boms")}
    if "kind" not in bom_columns:
        op.add_column("boms", sa.Column("kind", sa.String(10), nullable=False, server_default="ebom"))
    if "source_bom_id" not in bom_columns:
        op.add_column("boms", sa.Column("source_bom_id", GUID(), sa.ForeignKey("boms.id"), nullable=True))
    line_columns = {column["name"] for column in inspector.get_columns("bom_lines")}
    if "position" not in line_columns:
        op.add_column("bom_lines", sa.Column("position", sa.String(40)))
    if "reference_designator" not in line_columns:
        op.add_column("bom_lines", sa.Column("reference_designator", sa.String(100)))
    if "variant" not in line_columns:
        op.add_column("bom_lines", sa.Column("variant", sa.String(100)))
    if "substitutes" not in line_columns:
        op.add_column("bom_lines", sa.Column("substitutes", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("bom_lines", "substitutes")
    op.drop_column("bom_lines", "variant")
    op.drop_column("bom_lines", "reference_designator")
    op.drop_column("bom_lines", "position")
    op.drop_column("boms", "source_bom_id")
    op.drop_column("boms", "kind")
