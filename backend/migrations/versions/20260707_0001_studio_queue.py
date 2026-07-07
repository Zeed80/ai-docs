"""Studio queue ledger and generation audit fields.

Guarded/idempotent: clean installs get these tables/columns from metadata, while
upgraded installs receive only the missing pieces.

Revision ID: 20260707_0001
Revises: 20260706_0001
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects import postgresql

from app.db.base import GUID

revision = "20260707_0001"
down_revision = "20260706_0001"
branch_labels = None
depends_on = None


def _pg_enum_has(bind, enum_name: str, value: str) -> bool:
    found = bind.execute(
        sa.text(
            """
            SELECT 1
            FROM pg_enum e
            JOIN pg_type t ON t.oid = e.enumtypid
            WHERE t.typname = :enum_name AND e.enumlabel = :value
            """
        ),
        {"enum_name": enum_name, "value": value},
    ).scalar()
    return bool(found)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa_inspect(bind)
    tables = set(insp.get_table_names())

    if bind.dialect.name == "postgresql" and not _pg_enum_has(bind, "imagegenstatus", "cancelled"):
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE imagegenstatus ADD VALUE 'cancelled'")

    if "image_generations" in tables:
        cols = {c["name"] for c in insp.get_columns("image_generations")}
        if "accepted_by" not in cols:
            op.add_column("image_generations", sa.Column("accepted_by", sa.String(255), nullable=True))
        if "accepted_at" not in cols:
            op.add_column("image_generations", sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
        if "quality_rating" not in cols:
            op.add_column("image_generations", sa.Column("quality_rating", sa.SmallInteger(), nullable=True))
        if "issue_tags" not in cols:
            op.add_column(
                "image_generations",
                sa.Column("issue_tags", sa.JSON(), nullable=False, server_default="[]"),
            )
        if "review_notes" not in cols:
            op.add_column("image_generations", sa.Column("review_notes", sa.Text(), nullable=True))
        if "workflow_snapshot" not in cols:
            op.add_column(
                "image_generations",
                sa.Column("workflow_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            )

    if "studio_jobs" not in tables:
        status_enum = sa.Enum(
            "queued",
            "waiting_resource",
            "running",
            "cancel_requested",
            "cancelled",
            "done",
            "failed",
            name="studiojobstatus",
        )
        kind_enum = sa.Enum(
            "image_generation",
            "lora_training",
            name="studiojobkind",
        )
        if bind.dialect.name == "postgresql":
            status_enum = postgresql.ENUM(
                "queued",
                "waiting_resource",
                "running",
                "cancel_requested",
                "cancelled",
                "done",
                "failed",
                name="studiojobstatus",
                create_type=False,
            )
            kind_enum = postgresql.ENUM(
                "image_generation",
                "lora_training",
                name="studiojobkind",
                create_type=False,
            )
            status_enum.create(bind, checkfirst=True)
            kind_enum.create(bind, checkfirst=True)
        op.create_table(
            "studio_jobs",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("owner_sub", sa.String(255), nullable=True),
            sa.Column("kind", kind_enum, nullable=False),
            sa.Column("status", status_enum, nullable=False, server_default="queued"),
            sa.Column("resource", sa.String(80), nullable=False, server_default="comfyui"),
            sa.Column("title", sa.String(300), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("generation_id", GUID(), nullable=True),
            sa.Column("lora_run_id", GUID(), nullable=True),
            sa.Column("celery_task_id", sa.String(200), nullable=True),
            sa.Column("progress", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["generation_id"], ["image_generations.id"]),
            sa.ForeignKeyConstraint(["lora_run_id"], ["lora_training_runs.id"]),
        )
        op.create_index("ix_studio_jobs_owner_sub", "studio_jobs", ["owner_sub"])
        op.create_index("ix_studio_jobs_kind", "studio_jobs", ["kind"])
        op.create_index("ix_studio_jobs_status", "studio_jobs", ["status"])
        op.create_index("ix_studio_jobs_resource", "studio_jobs", ["resource"])
        op.create_index("ix_studio_jobs_generation_id", "studio_jobs", ["generation_id"])
        op.create_index("ix_studio_jobs_lora_run_id", "studio_jobs", ["lora_run_id"])
        op.create_index("ix_studio_jobs_owner_status", "studio_jobs", ["owner_sub", "status"])
        op.create_index("ix_studio_jobs_resource_status", "studio_jobs", ["resource", "status"])
        op.create_index("ix_studio_jobs_priority_created", "studio_jobs", ["priority", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa_inspect(bind)
    tables = set(insp.get_table_names())

    if "studio_jobs" in tables:
        op.drop_table("studio_jobs")
        if bind.dialect.name == "postgresql":
            sa.Enum(name="studiojobstatus").drop(bind, checkfirst=True)
            sa.Enum(name="studiojobkind").drop(bind, checkfirst=True)

    if "image_generations" in tables:
        cols = {c["name"] for c in sa_inspect(bind).get_columns("image_generations")}
        for col in (
            "workflow_snapshot",
            "review_notes",
            "issue_tags",
            "quality_rating",
            "accepted_at",
            "accepted_by",
        ):
            if col in cols:
                op.drop_column("image_generations", col)
