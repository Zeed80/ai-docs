"""Per-user section access allowlist.

Adds ``users.section_access`` (JSON, nullable): an explicit allowlist of
workspace section keys an admin grants a user. NULL means "base sections only"
for regular users; admins bypass it entirely.

Revision ID: 20260724_0001
Revises: 20260721_0001
Create Date: 2026-07-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260724_0001"
down_revision = "20260721_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if "users" not in set(insp.get_table_names()):
        return
    columns = {c["name"] for c in insp.get_columns("users")}
    if "section_access" not in columns:
        op.add_column("users", sa.Column("section_access", sa.JSON(), nullable=True))


def downgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if "users" not in set(insp.get_table_names()):
        return
    columns = {c["name"] for c in insp.get_columns("users")}
    if "section_access" in columns:
        op.drop_column("users", "section_access")
