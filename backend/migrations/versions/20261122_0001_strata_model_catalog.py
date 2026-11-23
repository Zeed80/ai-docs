"""Versioned official Strata model catalog snapshots.

Revision ID: 20261122_0001
Revises: 20261020_0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20261122_0001"
down_revision = "20261020_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not op.get_context().as_sql and sa_inspect(op.get_bind()).has_table(
        "strata_catalog_snapshots"
    ):
        return
    op.create_table(
        "strata_catalog_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_revision", sa.String(500), nullable=False),
        sa.Column("feed_revision", sa.String(120)),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.UniqueConstraint("sha256", name="uq_strata_catalog_snapshots_sha256"),
    )
    op.create_index(
        "ix_strata_catalog_snapshots_source_revision",
        "strata_catalog_snapshots",
        ["source_revision"],
    )
    op.create_index(
        "ix_strata_catalog_snapshots_fetched_at",
        "strata_catalog_snapshots",
        ["fetched_at"],
    )


def downgrade() -> None:
    if not op.get_context().as_sql and not sa_inspect(op.get_bind()).has_table(
        "strata_catalog_snapshots"
    ):
        return
    op.drop_index("ix_strata_catalog_snapshots_fetched_at", table_name="strata_catalog_snapshots")
    op.drop_index(
        "ix_strata_catalog_snapshots_source_revision", table_name="strata_catalog_snapshots"
    )
    op.drop_table("strata_catalog_snapshots")
