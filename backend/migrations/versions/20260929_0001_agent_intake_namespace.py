"""Namespace durable-chat idempotency by verified intake channel."""

import sqlalchemy as sa
from alembic import op

revision = "20260929_0001"
down_revision = "20260927_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "durable_chat_runs",
        sa.Column("intake_channel", sa.String(80), nullable=False, server_default="http"),
    )
    op.add_column(
        "durable_chat_runs",
        sa.Column("external_message_id", sa.String(300), nullable=True),
    )
    op.execute("UPDATE durable_chat_runs SET external_message_id = request_id::text")
    op.alter_column("durable_chat_runs", "external_message_id", nullable=False)
    op.drop_constraint("uq_chat_run_request", "durable_chat_runs", type_="unique")
    op.create_unique_constraint(
        "uq_chat_run_intake_message",
        "durable_chat_runs",
        ["intake_channel", "owner_key", "external_message_id"],
    )
    op.alter_column("durable_chat_runs", "intake_channel", server_default=None)


def downgrade():
    op.drop_constraint("uq_chat_run_intake_message", "durable_chat_runs", type_="unique")
    op.create_unique_constraint(
        "uq_chat_run_request", "durable_chat_runs", ["owner_key", "request_id"]
    )
    op.drop_column("durable_chat_runs", "external_message_id")
    op.drop_column("durable_chat_runs", "intake_channel")
