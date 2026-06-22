"""Backfill: move historical AI free-text from notes -> special_marks.

Before the notes/special_marks split (20260621_0001) the extractor wrote all
free-form invoice text (delivery/payment conditions, etc.) into ``notes``.
``notes`` is now a user-only field, so legacy invoices still show that AI text
in the "Примечание" column. This migration moves it into ``special_marks`` and
clears ``notes`` — but only for rows that have NOT been touched by the new
pipeline yet (``special_marks IS NULL``), so genuine post-split user notes are
left untouched.

Revision ID: 20260622_0001
Revises: 20260621_0001
Create Date: 2026-06-22
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

revision = "20260622_0001"
down_revision = "20260621_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa_inspect(bind).get_columns("invoices")}
    if "special_marks" not in cols or "notes" not in cols:
        return
    bind.execute(
        text(
            """
            UPDATE invoices
               SET special_marks = notes,
                   notes = NULL
             WHERE special_marks IS NULL
               AND notes IS NOT NULL
               AND notes <> ''
            """
        )
    )


def downgrade() -> None:
    # Irreversible by design (the original notes/special_marks split is lost).
    pass
