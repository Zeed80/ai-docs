"""email filter rules engine

Server-side "if a message matches X, do Y" rules, evaluated on IMAP ingest.

Revision ID: 20260828_0002
Revises: 20260828_0001
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "20260828_0002"
down_revision: str | None = "20260828_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_rules",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("mailbox", sa.String(100), nullable=True),
        sa.Column("owner_sub", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("stop_processing", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_rules_mailbox", "email_rules", ["mailbox"])
    op.create_index("ix_email_rules_owner_sub", "email_rules", ["owner_sub"])

    op.create_table(
        "email_rule_logs",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("rule_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("actions_applied", sa.JSON(), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["rule_id"], ["email_rules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["email_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_rule_logs_rule_id", "email_rule_logs", ["rule_id"])


def downgrade() -> None:
    op.drop_table("email_rule_logs")
    op.drop_table("email_rules")
