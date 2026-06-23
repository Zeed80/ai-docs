"""Ad-hoc editable spreadsheets (workspace_sheets).

Guarded/idempotent: on a clean install the baseline create_all() already builds
the table from the model, so this migration must be a no-op when the table
exists (see project_migration_baseline_idempotency).

Revision ID: 20260623_0001
Revises: 20260622_0001
Create Date: 2026-06-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260623_0001"
down_revision = "20260622_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa_inspect(bind).get_table_names())
    if "workspace_sheets" in existing:
        return
    op.create_table(
        "workspace_sheets",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("owner_sub", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("columns", sa.JSON(), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        # server_default mirrors TimestampMixin so inserts that omit timestamps
        # (ORM relies on the DB default) don't violate the NOT NULL constraint.
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index(
        "ix_workspace_sheets_owner_sub", "workspace_sheets", ["owner_sub"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa_inspect(bind).get_table_names())
    if "workspace_sheets" not in existing:
        return
    op.drop_index("ix_workspace_sheets_owner_sub", table_name="workspace_sheets")
    op.drop_table("workspace_sheets")
