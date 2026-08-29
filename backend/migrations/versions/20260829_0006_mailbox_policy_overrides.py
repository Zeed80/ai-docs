"""Ф9: per-mailbox overrides of the global mail policy.

Auto-send, its daily cap and the attachment size limit were one global triple:
"автоответы для ящика рекламаций" and "никаких автоответов из личной почты"
were the same switch. NULL means inherit, so existing mailboxes keep the exact
behaviour they had.

Revision ID: 20260829_0006
Revises: 20260829_0005
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_0006"
down_revision: Union[str, None] = "20260829_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("auto_send_enabled", sa.Boolean()),
    ("auto_send_max_per_day", sa.Integer()),
    ("max_attachment_mb", sa.Integer()),
)


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("mailbox_configs")}
    for name, type_ in _COLUMNS:
        if name not in have:
            op.add_column("mailbox_configs", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _type in _COLUMNS:
        op.drop_column("mailbox_configs", name)
