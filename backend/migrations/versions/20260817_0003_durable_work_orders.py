"""Add durable autonomous work-order runtime.

Revision ID: 20260817_0003
Revises: 20260817_0002
Create Date: 2026-08-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260817_0003"
down_revision = "20260817_0002"
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
        "work_orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_key", sa.String(200), nullable=False),
        sa.Column("source", sa.String(50), server_default="api", nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(40), server_default="received", nullable=False),
        sa.Column("risk_level", sa.String(20), server_default="low", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="50", nullable=False),
        sa.Column("constraints", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("budgets", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column(
            "policy_snapshot", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False
        ),
        sa.Column("plan_revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("result_summary", sa.Text()),
        sa.Column("blocker", sa.JSON()),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("canceled_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("parent_id", sa.Uuid()),
        sa.Column("legacy_agent_task_id", sa.Uuid()),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["parent_id"], ["work_orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["legacy_agent_task_id"], ["agent_tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("legacy_agent_task_id"),
    )
    op.create_index("ix_work_orders_owner_key", "work_orders", ["owner_key"])
    op.create_index("ix_work_orders_source", "work_orders", ["source"])
    op.create_index("ix_work_orders_status", "work_orders", ["status"])
    op.create_index("ix_work_orders_deadline_at", "work_orders", ["deadline_at"])
    op.create_index("ix_work_orders_lease_owner", "work_orders", ["lease_owner"])
    op.create_index("ix_work_orders_lease_expires_at", "work_orders", ["lease_expires_at"])
    op.create_index("ix_work_orders_parent_id", "work_orders", ["parent_id"])
    op.create_index("ix_work_orders_dispatch", "work_orders", ["status", "priority", "created_at"])

    op.create_table(
        "work_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_order_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("assumptions", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column(
            "verification_plan", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False
        ),
        sa.Column("created_by", sa.String(100), server_default="planner", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_order_id", "revision", name="uq_work_plan_revision"),
    )
    op.create_index("ix_work_plans_work_order_id", "work_plans", ["work_order_id"])
    op.create_index("ix_work_plans_status", "work_plans", ["status"])

    op.create_table(
        "work_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_order_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("step_key", sa.String(120), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("capability", sa.String(200)),
        sa.Column("action", sa.String(200)),
        sa.Column("input", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("depends_on", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column(
            "success_predicate", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False
        ),
        sa.Column("state", sa.String(40), server_default="pending", nullable=False),
        sa.Column("risk_level", sa.String(20), server_default="low", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default="600", nullable=False),
        sa.Column("retry_policy", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("output", sa.JSON()),
        sa.Column("last_error", sa.JSON()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["work_plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "step_key", name="uq_work_step_key"),
        sa.UniqueConstraint("idempotency_key", name="uq_work_step_idempotency_key"),
    )
    for column in (
        "work_order_id",
        "plan_id",
        "kind",
        "state",
        "next_attempt_at",
        "lease_owner",
        "lease_expires_at",
    ):
        op.create_index(f"ix_work_steps_{column}", "work_steps", [column])
    op.create_index(
        "ix_work_steps_dispatch", "work_steps", ["state", "next_attempt_at", "created_at"]
    )

    op.create_table(
        "work_step_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("step_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(200), nullable=False),
        sa.Column("status", sa.String(30), server_default="running", nullable=False),
        sa.Column("input", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("output", sa.JSON()),
        sa.Column("checkpoint", sa.JSON()),
        sa.Column("error", sa.JSON()),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["step_id"], ["work_steps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("step_id", "attempt_no", name="uq_work_step_attempt"),
    )
    op.create_index("ix_work_step_attempts_step_id", "work_step_attempts", ["step_id"])
    op.create_index("ix_work_step_attempts_status", "work_step_attempts", ["status"])

    op.create_table(
        "work_acceptance_criteria",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_order_id", sa.Uuid(), nullable=False),
        sa.Column("criterion_key", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("required", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("predicate", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("status", sa.String(30), server_default="pending", nullable=False),
        sa.Column("verdict", sa.JSON()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("verified_by", sa.String(200)),
        *_timestamps(),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_order_id", "criterion_key", name="uq_work_criterion_key"),
    )
    op.create_index(
        "ix_work_acceptance_criteria_work_order_id",
        "work_acceptance_criteria",
        ["work_order_id"],
    )
    op.create_index("ix_work_acceptance_criteria_status", "work_acceptance_criteria", ["status"])

    op.create_table(
        "work_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_order_id", sa.Uuid(), nullable=False),
        sa.Column("step_id", sa.Uuid()),
        sa.Column("artifact_type", sa.String(50), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("uri", sa.Text()),
        sa.Column("content_hash", sa.String(128)),
        sa.Column("content_type", sa.String(150)),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["work_steps.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("work_order_id", "step_id", "artifact_type", "content_hash"):
        op.create_index(f"ix_work_artifacts_{column}", "work_artifacts", [column])

    op.create_table(
        "work_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_order_id", sa.Uuid(), nullable=False),
        sa.Column("criterion_id", sa.Uuid()),
        sa.Column("step_id", sa.Uuid()),
        sa.Column("evidence_type", sa.String(50), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("payload", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("content_hash", sa.String(128)),
        sa.Column("verifier_status", sa.String(30), server_default="unverified", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["criterion_id"], ["work_acceptance_criteria.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["step_id"], ["work_steps.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("work_order_id", "criterion_id", "step_id", "content_hash"):
        op.create_index(f"ix_work_evidence_{column}", "work_evidence", [column])

    op.create_table(
        "work_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_order_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("payload", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_order_id", "sequence", name="uq_work_events_order_sequence"),
    )
    op.create_index("ix_work_events_work_order_id", "work_events", ["work_order_id"])
    op.create_index("ix_work_events_event_type", "work_events", ["event_type"])
    op.create_index("ix_work_events_created_at", "work_events", ["created_at"])
    op.create_index("ix_work_events_order_seq", "work_events", ["work_order_id", "sequence"])


def downgrade() -> None:
    for table in (
        "work_events",
        "work_evidence",
        "work_artifacts",
        "work_acceptance_criteria",
        "work_step_attempts",
        "work_steps",
        "work_plans",
        "work_orders",
    ):
        op.drop_table(table)
