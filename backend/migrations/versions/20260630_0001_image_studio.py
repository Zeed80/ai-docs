"""Image studio: comfyui_workflows + image_generations.

Guarded/idempotent: on a clean install the baseline create_all() already builds
these tables from the models, so each create is a no-op when the table exists
(see project_migration_baseline_idempotency).

Revision ID: 20260630_0001
Revises: 20260626_0001
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects import postgresql

from app.db.base import GUID

revision = "20260630_0001"
down_revision = "20260626_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa_inspect(bind).get_table_names())

    if "comfyui_workflows" not in existing:
        op.create_table(
            "comfyui_workflows",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("key", sa.String(length=120), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("category", sa.String(length=60), nullable=False, server_default="edit"),
            sa.Column("operation", sa.String(length=60), nullable=False, server_default="edit"),
            sa.Column("graph", sa.JSON(), nullable=False),
            sa.Column("inject_map", sa.JSON(), nullable=False),
            sa.Column("params_schema", sa.JSON(), nullable=False),
            sa.Column("thumbnail_path", sa.String(length=1000), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("owner_sub", sa.String(length=255), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.text("now()"), nullable=False,
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True),
                server_default=sa.text("now()"), nullable=False,
            ),
        )
        op.create_index("ix_comfyui_workflows_key", "comfyui_workflows", ["key"])
        op.create_index("ix_comfyui_workflows_category", "comfyui_workflows", ["category"])
        op.create_index("ix_comfyui_workflows_is_builtin", "comfyui_workflows", ["is_builtin"])
        op.create_index("ix_comfyui_workflows_owner_sub", "comfyui_workflows", ["owner_sub"])

    if "image_generations" not in existing:
        # Create the enum type idempotently, and bind the column to it with
        # create_type=False so create_table does NOT re-emit an un-guarded
        # CREATE TYPE (which fails with DuplicateObject). postgresql.ENUM honours
        # create_type=False reliably.
        status_enum = postgresql.ENUM(
            "queued", "running", "done", "failed",
            name="imagegenstatus", create_type=False,
        )
        status_enum.create(bind, checkfirst=True)
        op.create_table(
            "image_generations",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("owner_sub", sa.String(length=255), nullable=True),
            sa.Column("operation", sa.String(length=60), nullable=False, server_default="edit"),
            sa.Column("workflow_id", GUID(), nullable=True),
            sa.Column(
                "status", status_enum, nullable=False, server_default="queued"
            ),
            sa.Column("prompt", sa.Text(), nullable=True),
            sa.Column("negative_prompt", sa.Text(), nullable=True),
            sa.Column("params", sa.JSON(), nullable=False),
            sa.Column("source_image_paths", sa.JSON(), nullable=False),
            sa.Column("mask_path", sa.String(length=1000), nullable=True),
            sa.Column("result_path", sa.String(length=1000), nullable=True),
            sa.Column("thumbnail_path", sa.String(length=1000), nullable=True),
            sa.Column("comfyui_prompt_id", sa.String(length=200), nullable=True),
            sa.Column("celery_task_id", sa.String(length=200), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("parent_id", GUID(), nullable=True),
            sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.text("now()"), nullable=False,
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True),
                server_default=sa.text("now()"), nullable=False,
            ),
            sa.ForeignKeyConstraint(["workflow_id"], ["comfyui_workflows.id"]),
            sa.ForeignKeyConstraint(["parent_id"], ["image_generations.id"]),
        )
        op.create_index("ix_image_generations_owner_sub", "image_generations", ["owner_sub"])
        op.create_index("ix_image_generations_status", "image_generations", ["status"])
        op.create_index("ix_image_generations_parent_id", "image_generations", ["parent_id"])
        op.create_index(
            "ix_image_generations_owner_status", "image_generations", ["owner_sub", "status"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa_inspect(bind).get_table_names())
    if "image_generations" in existing:
        op.drop_table("image_generations")
        sa.Enum(name="imagegenstatus").drop(bind, checkfirst=True)
    if "comfyui_workflows" in existing:
        op.drop_table("comfyui_workflows")
