"""Persistent model runtime config and assignment revisions.

Revision ID: 20260619_0003
Revises: 20260619_0002
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260619_0003"
down_revision = "20260619_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())

    if not insp.has_table("model_catalog_runtime_entries"):
        op.create_table(
            "model_catalog_runtime_entries",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("model_key", sa.String(240), nullable=False),
            sa.Column("provider", sa.String(50), nullable=False),
            sa.Column("provider_model", sa.String(500), nullable=False),
            sa.Column("capability", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("source", sa.String(50), nullable=False, server_default="discovered"),
            sa.Column("verification_status", sa.String(40), nullable=False, server_default="discovered"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("model_key", name="uq_model_catalog_runtime_entries_key"),
        )
        op.create_index(
            "ix_model_catalog_runtime_entries_provider",
            "model_catalog_runtime_entries",
            ["provider"],
        )

    if not insp.has_table("model_runtime_overrides"):
        op.create_table(
            "model_runtime_overrides",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("model_key", sa.String(240), nullable=False),
            sa.Column("thinking_enabled", sa.Boolean(), nullable=True),
            sa.Column("preferred_instance", sa.String(150), nullable=True),
            sa.Column("verification_status", sa.String(40), nullable=False, server_default="discovered"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("model_key", name="uq_model_runtime_overrides_key"),
        )
        op.create_index(
            "ix_model_runtime_overrides_status",
            "model_runtime_overrides",
            ["verification_status"],
        )

    if not insp.has_table("model_assignment_revisions"):
        op.create_table(
            "model_assignment_revisions",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_by", sa.String(255), nullable=False, server_default="system"),
            sa.Column("before_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("after_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("diff", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
            sa.Column("warnings", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_model_assignment_revisions_created_at",
            "model_assignment_revisions",
            ["created_at"],
        )
        op.create_index(
            "ix_model_assignment_revisions_created_by",
            "model_assignment_revisions",
            ["created_by"],
        )


def downgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if insp.has_table("model_assignment_revisions"):
        op.drop_index("ix_model_assignment_revisions_created_by", table_name="model_assignment_revisions")
        op.drop_index("ix_model_assignment_revisions_created_at", table_name="model_assignment_revisions")
        op.drop_table("model_assignment_revisions")
    if insp.has_table("model_runtime_overrides"):
        op.drop_index("ix_model_runtime_overrides_status", table_name="model_runtime_overrides")
        op.drop_table("model_runtime_overrides")
    if insp.has_table("model_catalog_runtime_entries"):
        op.drop_index("ix_model_catalog_runtime_entries_provider", table_name="model_catalog_runtime_entries")
        op.drop_table("model_catalog_runtime_entries")
