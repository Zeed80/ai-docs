"""Bind CAD approval to an immutable, content-addressed IR revision.

Revision ID: 20260711_0001
Revises: 20260710_0001
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260711_0001"
down_revision = "20260710_0001"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa_inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    image_columns = _columns("image_generations")
    if "accepted_revision" not in image_columns:
        op.add_column("image_generations", sa.Column("accepted_revision", sa.Integer(), nullable=True))

    revision_columns = _columns("cad_ir_revisions")
    if "ir_sha256" not in revision_columns:
        op.add_column("cad_ir_revisions", sa.Column("ir_sha256", sa.String(64), nullable=True))
    if "artifact_hashes" not in revision_columns:
        op.add_column(
            "cad_ir_revisions",
            sa.Column("artifact_hashes", sa.JSON(), nullable=False, server_default="{}"),
        )
    if "approved_by" not in revision_columns:
        op.add_column("cad_ir_revisions", sa.Column("approved_by", sa.String(255), nullable=True))
    if "approved_at" not in revision_columns:
        op.add_column(
            "cad_ir_revisions",
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    revision_columns = _columns("cad_ir_revisions")
    for name in ("approved_at", "approved_by", "artifact_hashes", "ir_sha256"):
        if name in revision_columns:
            op.drop_column("cad_ir_revisions", name)
    if "accepted_revision" in _columns("image_generations"):
        op.drop_column("image_generations", "accepted_revision")
