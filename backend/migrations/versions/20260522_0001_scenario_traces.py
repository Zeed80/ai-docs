"""Epic 8C: scenario_traces table for agent execution tracing.

Revision ID: 20260522_0001
Revises: 20260520_0001
Create Date: 2026-05-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260522_0001"
down_revision = "20260520_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scenario_traces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("scenario_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="ok"),
        sa.Column("trigger", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("steps_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("steps_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("step_traces", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_by", sa.String(), nullable=False, server_default="system"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scenario_traces_scenario_name", "scenario_traces", ["scenario_name"])
    op.create_index("ix_scenario_traces_started_at", "scenario_traces", ["started_at"])
    op.create_index("ix_scenario_traces_status", "scenario_traces", ["status"])


def downgrade() -> None:
    op.drop_index("ix_scenario_traces_status", table_name="scenario_traces")
    op.drop_index("ix_scenario_traces_started_at", table_name="scenario_traces")
    op.drop_index("ix_scenario_traces_scenario_name", table_name="scenario_traces")
    op.drop_table("scenario_traces")
