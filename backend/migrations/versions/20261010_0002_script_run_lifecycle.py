"""E29: script run lifecycle columns on agent_script_runs.

The table existed unused (no rows). Adds the logical key that makes a retried
attempt find its run instead of starting a second job, and the run's inputs,
limits, result and outputs.

Revision ID: 20261010_0002
Revises: 20261010_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "20261010_0002"
down_revision = "20261010_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_script_runs") as batch:
        batch.add_column(sa.Column("logical_key", sa.String(300), nullable=True))
        batch.add_column(sa.Column("step_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("attempt_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("code_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("runtime", sa.String(60), nullable=True))
        batch.add_column(sa.Column("inputs", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("timeout_seconds", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("result", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("output_artifact_ids", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_agent_script_runs_logical_key", ["logical_key"])


def downgrade() -> None:
    with op.batch_alter_table("agent_script_runs") as batch:
        batch.drop_constraint("uq_agent_script_runs_logical_key", type_="unique")
        for column in (
            "finished_at",
            "started_at",
            "output_artifact_ids",
            "result",
            "timeout_seconds",
            "inputs",
            "runtime",
            "code_sha256",
            "attempt_id",
            "step_id",
            "logical_key",
        ):
            batch.drop_column(column)
