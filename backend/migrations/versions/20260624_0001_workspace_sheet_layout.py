"""Add layout metadata to workspace sheets.

Revision ID: 20260624_0001
Revises: 20260623_0001
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260624_0001"
down_revision = "20260623_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa_inspect(bind).get_columns("workspace_sheets")}
    if "layout" not in columns:
        op.add_column(
            "workspace_sheets",
            sa.Column(
                "layout",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
        op.alter_column("workspace_sheets", "layout", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa_inspect(bind).get_columns("workspace_sheets")}
    if "layout" in columns:
        op.drop_column("workspace_sheets", "layout")
