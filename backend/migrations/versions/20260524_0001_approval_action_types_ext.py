"""Add email.send.request_approval and tech.process_plan_from_drawing to ApprovalActionType."""

from alembic import op

revision = "20260524_0001"
down_revision = "20260523_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL makes a newly added enum value visible only after the
    # transaction commits.  Later migrations inspect this enum, therefore
    # these DDL statements must be committed before Alembic continues.
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE approvalactiontype ADD VALUE IF NOT EXISTS 'email.send.request_approval'")
            op.execute("ALTER TYPE approvalactiontype ADD VALUE IF NOT EXISTS 'tech.process_plan_from_drawing'")
    else:
        op.execute("ALTER TYPE approvalactiontype ADD VALUE IF NOT EXISTS 'email.send.request_approval'")
        op.execute("ALTER TYPE approvalactiontype ADD VALUE IF NOT EXISTS 'tech.process_plan_from_drawing'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; downgrade is a no-op.
    pass
