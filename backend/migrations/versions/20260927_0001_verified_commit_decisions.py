"""Reserve one verified-commit continuation per source action and attempt."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260927_0001"
down_revision = "20260922_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "verified_commit_decisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "work_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("work_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_attempt_id",
            UUID(as_uuid=True),
            sa.ForeignKey("work_step_attempts.id"),
            nullable=False,
        ),
        sa.Column(
            "logical_action_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_logical_actions.id"),
            nullable=False,
        ),
        sa.Column("owner_key", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("source_checkpoint_sha256", sa.String(64), nullable=False),
        sa.Column("source_plan_revision", sa.Integer(), nullable=False),
        sa.Column("observation", sa.JSON(), nullable=False),
        sa.Column("restored_checkpoint", sa.JSON()),
        sa.Column("event_id", UUID(as_uuid=True), sa.ForeignKey("work_events.id"), nullable=False),
        sa.Column("target_step_id", UUID(as_uuid=True), sa.ForeignKey("work_steps.id")),
        sa.Column("target_revision", sa.Integer()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("source_attempt_id", name="uq_verified_commit_source_attempt"),
        sa.UniqueConstraint("logical_action_id", name="uq_verified_commit_logical_action"),
        sa.UniqueConstraint("target_step_id", name="uq_verified_commit_target_step"),
    )
    op.create_index(
        "ix_verified_commit_decisions_work_order_id", "verified_commit_decisions", ["work_order_id"]
    )


def downgrade():
    op.drop_table("verified_commit_decisions")
