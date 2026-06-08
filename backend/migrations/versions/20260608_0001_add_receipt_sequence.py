"""Add receipt_seq PostgreSQL sequence for race-free receipt number generation.

Revision ID: 20260608_0001
Revises: 20260602_0001
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "20260608_0001"
down_revision = "20260602_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(
        text("SELECT 1 FROM pg_sequences WHERE sequencename = 'receipt_seq'")
    ).scalar()
    if not exists:
        try:
            max_num = conn.execute(
                text("SELECT COUNT(*) FROM warehouse_receipts")
            ).scalar() or 0
        except Exception:
            max_num = 0
        start = max(max_num + 1, 1)
        conn.execute(text(f"CREATE SEQUENCE receipt_seq START {start}"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP SEQUENCE IF EXISTS receipt_seq"))
