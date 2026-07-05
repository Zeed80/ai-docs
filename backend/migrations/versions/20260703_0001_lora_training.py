"""LoRA training feature: lora_datasets + lora_training_runs tables and the
lora_train approval action type.

Guarded/idempotent — clean installs get the tables from the baseline
create_all, so everything here checks existence first (see
project_migration_baseline_idempotency).

Revision ID: 20260703_0001
Revises: 20260701_0001
Create Date: 2026-07-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260703_0001"
down_revision = "20260701_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if "lora_datasets" not in tables:
        op.create_table(
            "lora_datasets",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("owner_sub", sa.String(255), nullable=True, index=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column(
                "status",
                sa.Enum("preparing", "ready", "failed", name="loradatasetstatus"),
                nullable=False,
                index=True,
            ),
            sa.Column("preset", sa.String(60), nullable=False),
            sa.Column("params", sa.JSON(), nullable=False),
            sa.Column("source_paths", sa.JSON(), nullable=False),
            sa.Column("dataset_dir", sa.String(1000), nullable=True),
            sa.Column("stats", sa.JSON(), nullable=False),
            sa.Column("preview_paths", sa.JSON(), nullable=False),
            sa.Column("celery_task_id", sa.String(200), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
        )

    if "lora_training_runs" not in tables:
        op.create_table(
            "lora_training_runs",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("owner_sub", sa.String(255), nullable=True, index=True),
            sa.Column(
                "dataset_id",
                GUID(),
                sa.ForeignKey("lora_datasets.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column(
                "status",
                sa.Enum(
                    "pending_approval", "queued", "running", "stopping",
                    "done", "failed", "cancelled",
                    name="lorarunstatus",
                ),
                nullable=False,
                index=True,
            ),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("progress", sa.JSON(), nullable=False),
            sa.Column("checkpoints", sa.JSON(), nullable=False),
            sa.Column("sample_paths", sa.JSON(), nullable=False),
            sa.Column("output_dir", sa.String(1000), nullable=True),
            sa.Column("container_id", sa.String(200), nullable=True),
            sa.Column("celery_task_id", sa.String(200), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
        )

    if bind.dialect.name == "postgresql":
        existing = bind.execute(
            sa.text("SELECT enum_range(NULL::approvalactiontype)::text")
        ).scalar()
        if "lora_train" not in (existing or ""):
            with op.get_context().autocommit_block():
                op.execute("ALTER TYPE approvalactiontype ADD VALUE 'lora_train'")


def downgrade() -> None:
    op.drop_table("lora_training_runs")
    op.drop_table("lora_datasets")
