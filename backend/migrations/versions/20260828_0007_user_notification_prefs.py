"""Ф0.8: server-side notification preferences (were localStorage-only).

Revision ID: 20260828_0007
Revises: 20260828_0006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "20260828_0007"
down_revision: Union[str, None] = "20260828_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("user_notification_prefs"):
        return
    op.create_table(
        "user_notification_prefs",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("user_sub", sa.String(255), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("in_app", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("push", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("private_preview", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_sub", "type", name="uq_user_notification_pref"),
    )
    op.create_index(
        "ix_user_notification_prefs_user_sub", "user_notification_prefs", ["user_sub"]
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("user_notification_prefs"):
        op.drop_index("ix_user_notification_prefs_user_sub", table_name="user_notification_prefs")
        op.drop_table("user_notification_prefs")
