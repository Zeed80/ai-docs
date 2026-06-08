"""Add receipt_seq PostgreSQL sequence for race-free receipt number generation.

Revision ID: 20260608_0001
Revises: a3b4c5d6e7f8, c6d7e8f9a0b1, e7f8a9b0c1d2
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op

revision: str = "20260608_0001"
down_revision = ("a3b4c5d6e7f8", "c6d7e8f9a0b1", "e7f8a9b0c1d2")
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotent: skip if sequence already exists
    exists = conn.execute(
        "SELECT 1 FROM pg_sequences WHERE sequencename = 'receipt_seq'"
    ).scalar()
    if not exists:
        # Start from current max receipt count + 1 so existing numbers stay valid
        try:
            max_num = conn.execute(
                "SELECT COUNT(*) FROM warehouse_receipts"
            ).scalar() or 0
        except Exception:
            max_num = 0
        start = max(max_num + 1, 1)
        conn.execute(f"CREATE SEQUENCE receipt_seq START {start}")


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute("DROP SEQUENCE IF EXISTS receipt_seq")
