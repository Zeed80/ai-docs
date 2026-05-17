"""Add share_token to chat_sessions.

Revision ID: e7f8a9b0c1d2
Revises: 2bc89634b9df
Create Date: 2026-05-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "e7f8a9b0c1d2"
down_revision: str = "2bc89634b9df"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    cols = [c["name"] for c in insp.get_columns("chat_sessions")]
    if "share_token" not in cols:
        op.add_column(
            "chat_sessions",
            sa.Column("share_token", sa.String(64), nullable=True, unique=True),
        )
        op.create_index(
            "ix_chat_sessions_share_token",
            "chat_sessions",
            ["share_token"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_share_token", table_name="chat_sessions")
    op.drop_column("chat_sessions", "share_token")
