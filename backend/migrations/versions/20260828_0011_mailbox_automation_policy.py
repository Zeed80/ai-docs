"""Ф6.1: per-mailbox automation policy for incoming attachments.

Revision ID: 20260828_0011
Revises: 20260828_0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0011"
down_revision: str | None = "20260828_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("mailbox_configs")}
    if "auto_process_attachments" not in cols:
        op.add_column(
            "mailbox_configs",
            sa.Column(
                "auto_process_attachments",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
    if "auto_approve_invoices" not in cols:
        op.add_column(
            "mailbox_configs",
            sa.Column(
                "auto_approve_invoices",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("mailbox_configs")}
    for name in ("auto_approve_invoices", "auto_process_attachments"):
        if name in cols:
            op.drop_column("mailbox_configs", name)
