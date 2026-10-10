"""E24: receipts of durable tool effects, written in the effect's own commit.

Revision ID: 20261010_0001
Revises: 20261007_0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261010_0001"
down_revision = "20261007_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_effect_receipts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_key", sa.String(500), nullable=False),
        sa.Column(
            "work_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("work_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_id", UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", UUID(as_uuid=True), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("operation_key", name="uq_work_effect_receipt_operation_key"),
    )
    op.create_index(
        "ix_work_effect_receipts_work_order_id", "work_effect_receipts", ["work_order_id"]
    )
    op.create_index("ix_work_effect_receipts_attempt_id", "work_effect_receipts", ["attempt_id"])


def downgrade() -> None:
    op.drop_table("work_effect_receipts")
