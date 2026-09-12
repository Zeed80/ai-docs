"""Persist logical chat actions independently of model call IDs and attempts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260912_0001"
down_revision = "20260911_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_logical_actions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "work_order_id", UUID(as_uuid=True), sa.ForeignKey("work_orders.id"), nullable=False
        ),
        sa.Column(
            "attempt_id", UUID(as_uuid=True), sa.ForeignKey("work_step_attempts.id"), nullable=False
        ),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("result_digest", sa.String(64)),
    )
    op.create_index(
        "ix_chat_logical_actions_work_order_id", "chat_logical_actions", ["work_order_id"]
    )


def downgrade():
    op.drop_table("chat_logical_actions")
