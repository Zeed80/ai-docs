"""image_generations: link to source document / work case (optional).

Guarded/idempotent — adds nullable columns only if absent, mirrors the pattern
used in 20260630_0001_image_studio.py (see project_migration_baseline_idempotency).

Revision ID: 20260701_0001
Revises: 20260630_0001
Create Date: 2026-07-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260701_0001"
down_revision = "20260630_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa_inspect(bind).get_columns("image_generations")}

    if "source_document_id" not in cols:
        op.add_column(
            "image_generations",
            sa.Column("source_document_id", GUID(), nullable=True),
        )
        op.create_foreign_key(
            "fk_image_generations_source_document_id",
            "image_generations",
            "documents",
            ["source_document_id"],
            ["id"],
        )
        op.create_index(
            "ix_image_generations_source_document_id",
            "image_generations",
            ["source_document_id"],
        )

    if "case_id" not in cols:
        op.add_column(
            "image_generations",
            sa.Column("case_id", GUID(), nullable=True),
        )
        op.create_foreign_key(
            "fk_image_generations_case_id",
            "image_generations",
            "work_cases",
            ["case_id"],
            ["id"],
        )
        op.create_index(
            "ix_image_generations_case_id",
            "image_generations",
            ["case_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa_inspect(bind).get_columns("image_generations")}

    if "case_id" in cols:
        op.drop_constraint(
            "fk_image_generations_case_id", "image_generations", type_="foreignkey"
        )
        op.drop_index("ix_image_generations_case_id", table_name="image_generations")
        op.drop_column("image_generations", "case_id")

    if "source_document_id" in cols:
        op.drop_constraint(
            "fk_image_generations_source_document_id", "image_generations", type_="foreignkey"
        )
        op.drop_index("ix_image_generations_source_document_id", table_name="image_generations")
        op.drop_column("image_generations", "source_document_id")
