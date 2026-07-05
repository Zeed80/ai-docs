"""LoRA training v2: base-model family (FLUX.2 support), sample-control
paths, run timestamps; approval gate removed (no schema change — enum values
kept for old rows).

Guarded/idempotent — clean installs get the columns from the baseline
create_all, so everything here checks existence first (see
project_migration_baseline_idempotency).

Revision ID: 20260705_0001
Revises: 20260703_0001
Create Date: 2026-07-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260705_0001"
down_revision = "20260703_0001"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa_inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if "lora_training_runs" in tables:
        cols = _columns(bind, "lora_training_runs")
        if "base_family" not in cols:
            op.add_column(
                "lora_training_runs",
                sa.Column("base_family", sa.String(30), nullable=False,
                          server_default="qwen"),
            )
        if "control_paths" not in cols:
            op.add_column(
                "lora_training_runs",
                sa.Column("control_paths", sa.JSON(), nullable=False,
                          server_default="[]"),
            )
        if "started_at" not in cols:
            op.add_column(
                "lora_training_runs",
                sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            )
        if "finished_at" not in cols:
            op.add_column(
                "lora_training_runs",
                sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            )

    if "comfyui_workflows" in tables:
        if "base_family" not in _columns(bind, "comfyui_workflows"):
            op.add_column(
                "comfyui_workflows",
                sa.Column("base_family", sa.String(30), nullable=False,
                          server_default="qwen"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if "lora_training_runs" in tables:
        cols = _columns(bind, "lora_training_runs")
        for col in ("finished_at", "started_at", "control_paths", "base_family"):
            if col in cols:
                op.drop_column("lora_training_runs", col)
    if "comfyui_workflows" in tables and "base_family" in _columns(bind, "comfyui_workflows"):
        op.drop_column("comfyui_workflows", "base_family")
