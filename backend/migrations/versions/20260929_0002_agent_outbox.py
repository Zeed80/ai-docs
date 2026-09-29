"""Persist transactional agent outbox rows; delivery remains disabled."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260929_0002"
down_revision = "20260929_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_outbox",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "work_event_id",
            UUID(as_uuid=True),
            sa.ForeignKey("work_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "work_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("work_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_key", sa.String(200), nullable=False),
        sa.Column(
            "destination_binding_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_channel_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_version", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("dedup_key", sa.String(300), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("delivery_state", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("lease_token", UUID(as_uuid=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(100)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "owner_key",
            "destination_binding_id",
            "dedup_key",
            name="uq_agent_outbox_destination_dedup",
        ),
        sa.UniqueConstraint("work_event_id", name="uq_agent_outbox_work_event"),
    )
    op.create_index(
        "ix_agent_outbox_delivery", "agent_outbox", ["delivery_state", "next_attempt_at"]
    )
    op.create_index("ix_agent_outbox_owner", "agent_outbox", ["owner_key"])
    op.create_index("ix_agent_outbox_work_order_id", "agent_outbox", ["work_order_id"])


def downgrade():
    op.drop_table("agent_outbox")
