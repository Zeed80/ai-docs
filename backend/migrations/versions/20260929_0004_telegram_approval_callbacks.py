"""Persist opaque Telegram approval callbacks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260929_0004"
down_revision = "20260929_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_outbox",
        sa.Column(
            "approval_id",
            UUID(as_uuid=True),
            sa.ForeignKey("approvals.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_agent_outbox_approval_id", "agent_outbox", ["approval_id"])
    op.create_table(
        "telegram_approval_callbacks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("token", sa.String(32), nullable=False),
        sa.Column(
            "approval_id",
            UUID(as_uuid=True),
            sa.ForeignKey("approvals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "binding_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_channel_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("owner_key", sa.String(200), nullable=False),
        sa.Column("action_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("token", name="uq_telegram_approval_callback_token"),
        sa.UniqueConstraint(
            "approval_id", "binding_id", name="uq_telegram_approval_callback_approval_binding"
        ),
    )
    op.create_index(
        "ix_telegram_approval_callbacks_approval", "telegram_approval_callbacks", ["approval_id"]
    )


def downgrade():
    op.drop_table("telegram_approval_callbacks")
    op.drop_index("ix_agent_outbox_approval_id", table_name="agent_outbox")
    op.drop_column("agent_outbox", "approval_id")
