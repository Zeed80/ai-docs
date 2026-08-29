"""Ф1.1: mark body_text that we rendered from HTML ourselves.

Revision ID: 20260828_0008
Revises: 20260828_0007
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0008"
down_revision: Union[str, None] = "20260828_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("email_messages")}
    if "body_text_derived" not in cols:
        op.add_column(
            "email_messages",
            sa.Column("body_text_derived", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("email_messages")}
    if "body_text_derived" in cols:
        op.drop_column("email_messages", "body_text_derived")
