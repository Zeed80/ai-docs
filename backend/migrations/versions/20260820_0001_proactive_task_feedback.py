"""Add proactive_task_feedback table + notifications.source_task.

Revision ID: 20260820_0001
Revises: 20260819_0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260820_0001"
down_revision = "20260819_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("source_task", sa.String(120)))
    op.create_index("ix_notifications_source_task", "notifications", ["source_task"])

    op.create_table(
        "proactive_task_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("beat_task_name", sa.String(120), nullable=False),
        sa.Column("notification_id", sa.Uuid()),
        sa.Column("entity_type", sa.String(50)),
        sa.Column("entity_id", sa.Uuid()),
        sa.Column("user_sub", sa.String(255), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("snoozed_until", sa.DateTime(timezone=True)),
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
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("beat_task_name", "notification_id", "user_sub"):
        op.create_index(f"ix_proactive_task_feedback_{column}", "proactive_task_feedback", [column])
    # Calibration queries filter by task + recency together.
    op.create_index(
        "ix_proactive_task_feedback_calibration",
        "proactive_task_feedback",
        ["beat_task_name", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_proactive_task_feedback_calibration", table_name="proactive_task_feedback")
    for column in ("beat_task_name", "notification_id", "user_sub"):
        op.drop_index(f"ix_proactive_task_feedback_{column}", table_name="proactive_task_feedback")
    op.drop_table("proactive_task_feedback")
    op.drop_index("ix_notifications_source_task", table_name="notifications")
    op.drop_column("notifications", "source_task")
