"""Add idempotent durable chat intake."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260911_0001"
down_revision = "20260910_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "durable_chat_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("owner_key", sa.String(200), nullable=False),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column(
            "work_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("work_orders.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "session_id", UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id"), nullable=False
        ),
        sa.Column(
            "user_message_id", UUID(as_uuid=True), sa.ForeignKey("chat_messages.id"), nullable=False
        ),
        sa.Column("result_message_id", UUID(as_uuid=True), sa.ForeignKey("chat_messages.id")),
        sa.UniqueConstraint("owner_key", "request_id", name="uq_chat_run_request"),
    )
    op.create_index("ix_durable_chat_runs_owner_key", "durable_chat_runs", ["owner_key"])
    op.create_index("ix_durable_chat_runs_session_id", "durable_chat_runs", ["session_id"])


def downgrade():
    op.drop_table("durable_chat_runs")
