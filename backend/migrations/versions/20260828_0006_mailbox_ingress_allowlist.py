"""Ф0.6: sender allowlist for the agent-instruction mailbox.

Revision ID: 20260828_0006
Revises: 20260828_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0006"
down_revision: str | None = "20260828_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("mailbox_configs")}
    if "ingress_allowed_senders" not in cols:
        op.add_column(
            "mailbox_configs", sa.Column("ingress_allowed_senders", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("mailbox_configs")}
    if "ingress_allowed_senders" in cols:
        op.drop_column("mailbox_configs", "ingress_allowed_senders")
