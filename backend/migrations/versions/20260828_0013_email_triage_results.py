"""Ф6.4: what the agent understood about an incoming letter.

Revision ID: 20260828_0013
Revises: 20260828_0012
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "20260828_0013"
down_revision: Union[str, None] = "20260828_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    cols = {c["name"] for c in insp.get_columns("mailbox_configs")}
    if "agent_triage_mode" not in cols:
        op.add_column(
            "mailbox_configs",
            sa.Column("agent_triage_mode", sa.String(20), nullable=False,
                      server_default="classify"),
        )

    if not insp.has_table("email_triage_results"):
        op.create_table(
            "email_triage_results",
            sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
            sa.Column("message_id", PG_UUID(as_uuid=True), nullable=False),
            sa.Column("mailbox", sa.String(255), nullable=False),
            sa.Column("category", sa.String(40), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("entities", sa.JSON(), nullable=True),
            sa.Column("proposed", sa.JSON(), nullable=True),
            sa.Column("performed", sa.JSON(), nullable=True),
            sa.Column("model_name", sa.String(120), nullable=True),
            sa.Column("work_order_id", PG_UUID(as_uuid=True), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="done"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("corrected_category", sa.String(40), nullable=True),
            sa.Column("corrected_by", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["message_id"], ["email_messages.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("message_id", name="uq_email_triage_message"),
        )
        op.create_index("ix_email_triage_results_mailbox", "email_triage_results", ["mailbox"])
        op.create_index("ix_email_triage_results_category", "email_triage_results", ["category"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("email_triage_results"):
        op.drop_table("email_triage_results")
    cols = {c["name"] for c in insp.get_columns("mailbox_configs")}
    if "agent_triage_mode" in cols:
        op.drop_column("mailbox_configs", "agent_triage_mode")
