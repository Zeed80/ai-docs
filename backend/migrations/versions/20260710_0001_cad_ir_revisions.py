"""CAD IR revision history for the studio vectorize/techdraw pipeline.

Guarded/idempotent: clean installs get the table from metadata (baseline
create_all), upgraded installs create it here only if missing.

Revision ID: 20260710_0001
Revises: 20260707_0001
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260710_0001"
down_revision = "20260707_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa_inspect(bind)
    tables = set(insp.get_table_names())

    if "cad_ir_revisions" not in tables:
        op.create_table(
            "cad_ir_revisions",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("generation_id", GUID(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ir_path", sa.String(1000), nullable=False),
            sa.Column("created_by", sa.String(255), nullable=True),
            sa.Column("origin", sa.String(30), nullable=False, server_default="auto"),
            sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"),
            sa.ForeignKeyConstraint(["generation_id"], ["image_generations.id"]),
        )
        op.create_index("ix_cad_ir_revisions_generation_id", "cad_ir_revisions", ["generation_id"])
        op.create_index(
            "ix_cad_ir_revisions_gen_rev",
            "cad_ir_revisions",
            ["generation_id", "revision"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa_inspect(bind)
    if "cad_ir_revisions" in set(insp.get_table_names()):
        op.drop_table("cad_ir_revisions")
