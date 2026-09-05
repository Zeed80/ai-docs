"""Persist resolved executor calls before side effects.

Revision ID: 20260817_0004
Revises: 20260817_0003
Create Date: 2026-08-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260817_0004"
down_revision = "20260817_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_tool_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_order_id", sa.Uuid(), nullable=False),
        sa.Column("step_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("call_no", sa.Integer(), nullable=False),
        sa.Column("executor", sa.String(40), nullable=False),
        sa.Column("capability", sa.String(200)),
        sa.Column("action", sa.String(200)),
        sa.Column("arguments", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("resolved_from", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("risk_level", sa.String(20), server_default="low", nullable=False),
        sa.Column("status", sa.String(30), server_default="prepared", nullable=False),
        sa.Column("action_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("output", sa.JSON()),
        sa.Column("error", sa.JSON()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
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
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["work_steps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id"], ["work_step_attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "call_no", name="uq_work_tool_call_attempt_no"),
        sa.UniqueConstraint("idempotency_key", name="uq_work_tool_call_idempotency_key"),
    )
    for column in ("work_order_id", "step_id", "attempt_id", "status", "action_digest"):
        op.create_index(f"ix_work_tool_calls_{column}", "work_tool_calls", [column])
    op.create_index(
        "ix_work_tool_calls_order_status", "work_tool_calls", ["work_order_id", "status"]
    )


def downgrade() -> None:
    op.drop_table("work_tool_calls")
