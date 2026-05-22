"""Add email.send.request_approval and tech.process_plan_from_drawing to ApprovalActionType."""

from alembic import op

revision = "20260524_0001"
down_revision = "20260523_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE approvalactiontype ADD VALUE IF NOT EXISTS 'email.send.request_approval'")
    op.execute("ALTER TYPE approvalactiontype ADD VALUE IF NOT EXISTS 'tech.process_plan_from_drawing'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; downgrade is a no-op.
    pass
