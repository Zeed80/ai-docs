"""Provider instances — multi-node AI providers + encrypted cloud API keys.

Revision ID: 20260614_0002
Revises: 20260614_0001
Create Date: 2026-06-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260614_0002"
down_revision = "20260614_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if not insp.has_table("provider_instances"):
        op.create_table(
            "provider_instances",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.String(50), nullable=False),
            sa.Column("name", sa.String(150), nullable=False),
            sa.Column("base_url", sa.String(500), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_local", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("api_key_encrypted", sa.Text(), nullable=True),
            sa.Column("extra", sa.JSON(), nullable=True),
            sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_check_ok", sa.Boolean(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
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
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", name="uq_provider_instances_name"),
        )
        op.create_index(
            "ix_provider_instances_kind", "provider_instances", ["kind"]
        )


def downgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if insp.has_table("provider_instances"):
        op.drop_index("ix_provider_instances_kind", table_name="provider_instances")
        op.drop_table("provider_instances")
