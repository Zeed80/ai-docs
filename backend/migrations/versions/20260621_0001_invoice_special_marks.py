"""Add special_marks column to invoices (AI-extracted; notes stays user-only).

Revision ID: 20260621_0001
Revises: 20260620_0001
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260621_0001"
down_revision = "20260620_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("invoices")}
    if "special_marks" not in cols:
        op.add_column("invoices", sa.Column("special_marks", sa.Text(), nullable=True))


def downgrade() -> None:
    insp = sa_inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("invoices")}
    if "special_marks" in cols:
        op.drop_column("invoices", "special_marks")
