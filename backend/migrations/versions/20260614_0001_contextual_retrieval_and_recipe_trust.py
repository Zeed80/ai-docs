"""Contextual Retrieval prefix + recipe passive-activation/trust counters.

Adds:
  - document_chunks.context_prefix (Contextual Retrieval — deterministic
    document context prepended to a chunk before embedding)
  - recipe_skills.worker_confirmations (passive activation: worker reproduced
    the recipe's exact steps N times → draft becomes active)
  - recipe_skills.confirmed_replays (explainable replay: human-approved replays
    before the recipe runs silently)

Idempotent (matches the project's inspect-guarded style): these columns may have
been added manually via ALTER TABLE on an already-running deployment, so each add
is guarded by a column-existence check. On a fresh database the columns are
created normally.

Revision ID: 20260614_0001
Revises: 20260611_0001
Create Date: 2026-06-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect


revision = "20260614_0001"
down_revision = "20260611_0001"
branch_labels = None
depends_on = None


def _has_column(insp, table: str, column: str) -> bool:
    if not insp.has_table(table):
        return False
    return any(col["name"] == column for col in insp.get_columns(table))


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())

    if insp.has_table("document_chunks") and not _has_column(
        insp, "document_chunks", "context_prefix"
    ):
        op.add_column(
            "document_chunks",
            sa.Column("context_prefix", sa.Text(), nullable=True),
        )

    if insp.has_table("recipe_skills"):
        if not _has_column(insp, "recipe_skills", "worker_confirmations"):
            op.add_column(
                "recipe_skills",
                sa.Column(
                    "worker_confirmations",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
            )
        if not _has_column(insp, "recipe_skills", "confirmed_replays"):
            op.add_column(
                "recipe_skills",
                sa.Column(
                    "confirmed_replays",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
            )


def downgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if _has_column(insp, "recipe_skills", "confirmed_replays"):
        op.drop_column("recipe_skills", "confirmed_replays")
    if _has_column(insp, "recipe_skills", "worker_confirmations"):
        op.drop_column("recipe_skills", "worker_confirmations")
    if _has_column(insp, "document_chunks", "context_prefix"):
        op.drop_column("document_chunks", "context_prefix")
