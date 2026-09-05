"""Add least-privilege computer-use grants and action audit.

Revision ID: 20260817_0005
Revises: 20260817_0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260817_0005"
down_revision = "20260817_0004"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "computer_use_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_order_id", sa.Uuid(), nullable=False),
        sa.Column("granted_to", sa.String(200), nullable=False),
        sa.Column("granted_by", sa.String(200), nullable=False),
        sa.Column("actions", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("allowed_roots", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("allowed_hosts", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column(
            "allowed_commands", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False
        ),
        sa.Column("max_actions", sa.Integer(), server_default="20", nullable=False),
        sa.Column("used_actions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.Text(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("work_order_id", "granted_to", "expires_at", "revoked_at"):
        op.create_index(f"ix_computer_use_grants_{column}", "computer_use_grants", [column])
    op.create_index(
        "ix_computer_use_grants_active",
        "computer_use_grants",
        ["work_order_id", "revoked_at", "expires_at"],
    )

    op.create_table(
        "computer_use_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_order_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("step_id", sa.Uuid()),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("arguments", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("action_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), server_default="prepared", nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.JSON()),
        sa.Column("evidence", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["grant_id"], ["computer_use_grants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["step_id"], ["work_steps.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("work_order_id", "grant_id", "step_id", "action", "action_digest", "status"):
        op.create_index(f"ix_computer_use_actions_{column}", "computer_use_actions", [column])
    op.create_index(
        "ix_computer_use_actions_order_created",
        "computer_use_actions",
        ["work_order_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("computer_use_actions")
    op.drop_table("computer_use_grants")
