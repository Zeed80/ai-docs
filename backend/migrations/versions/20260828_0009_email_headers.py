"""Ф1.2: keep the headers the parser was throwing away.

Revision ID: 20260828_0009
Revises: 20260828_0008
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0009"
down_revision: Union[str, None] = "20260828_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("email_messages")}
    if "reply_to" not in cols:
        op.add_column("email_messages", sa.Column("reply_to", sa.String(500), nullable=True))
    if "headers_meta" not in cols:
        op.add_column("email_messages", sa.Column("headers_meta", sa.JSON(), nullable=True))


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("email_messages")}
    if "headers_meta" in cols:
        op.drop_column("email_messages", "headers_meta")
    if "reply_to" in cols:
        op.drop_column("email_messages", "reply_to")
