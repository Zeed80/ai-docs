"""Ф8: per-mailbox retention for message bodies.

Until now only attachment BYTES had a retention window; subjects, addresses and
bodies were kept forever — including for personal mailboxes, where "forever" is
a privacy decision nobody made deliberately. Default stays 0 (keep forever), so
this migration changes no data: it only makes the decision expressible.

Revision ID: 20260829_0004
Revises: 20260829_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_0004"
down_revision: str | None = "20260829_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("mailbox_configs")}
    if "body_retention_days" not in cols:
        op.add_column(
            "mailbox_configs",
            sa.Column("body_retention_days", sa.Integer(), nullable=False, server_default="0"),
        )

    msg_cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("email_messages")}
    if "body_pruned_at" not in msg_cols:
        op.add_column(
            "email_messages",
            sa.Column("body_pruned_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("email_messages", "body_pruned_at")
    op.drop_column("mailbox_configs", "body_retention_days")
