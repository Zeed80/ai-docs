"""Ф3: thread assignment + an honest auto-reply ledger.

Revision ID: 20260828_0012
Revises: 20260828_0011
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "20260828_0012"
down_revision: Union[str, None] = "20260828_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    cols = {c["name"] for c in insp.get_columns("email_threads")}
    if "assigned_to_sub" not in cols:
        op.add_column("email_threads", sa.Column("assigned_to_sub", sa.String(255), nullable=True))
        op.create_index("ix_email_threads_assigned_to_sub", "email_threads", ["assigned_to_sub"])

    if not insp.has_table("email_auto_replies"):
        op.create_table(
            "email_auto_replies",
            sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
            sa.Column("rule_id", PG_UUID(as_uuid=True), nullable=True),
            sa.Column("draft_id", PG_UUID(as_uuid=True), nullable=True),
            sa.Column("in_reply_to_message_id", PG_UUID(as_uuid=True), nullable=True),
            sa.Column("mailbox", sa.String(255), nullable=True),
            sa.Column("recipient", sa.String(320), nullable=False),
            sa.Column("thread_root", sa.String(500), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["rule_id"], ["email_rules.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["draft_id"], ["draft_actions.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["in_reply_to_message_id"], ["email_messages.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        for col in ("rule_id", "mailbox", "recipient", "thread_root", "sent_at"):
            op.create_index(f"ix_email_auto_replies_{col}", "email_auto_replies", [col])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("email_auto_replies"):
        op.drop_table("email_auto_replies")
    cols = {c["name"] for c in insp.get_columns("email_threads")}
    if "assigned_to_sub" in cols:
        op.drop_index("ix_email_threads_assigned_to_sub", table_name="email_threads")
        op.drop_column("email_threads", "assigned_to_sub")
