"""Invoice keyword-search indexes — GIN FTS + trigram for supplier names,
item descriptions, invoice numbers and notes.

These back the invoices table keyword search (text_search_condition: russian
full-text + trigram similarity + ilike). Without them, FTS over invoice_lines
and parties does a sequential scan on large datasets.

Revision ID: 20260619_0001
Revises: 20260614_0002
Create Date: 2026-06-19
"""

from __future__ import annotations

from alembic import op

revision = "20260619_0001"
down_revision = "20260614_0002"
branch_labels = None
depends_on = None


_FTS_INDEXES = [
    (
        "ix_parties_name_fts_ru",
        "parties",
        "to_tsvector('russian', coalesce(name, ''))",
    ),
    (
        "ix_invoice_lines_description_fts_ru",
        "invoice_lines",
        "to_tsvector('russian', coalesce(description, ''))",
    ),
    (
        "ix_invoices_number_notes_fts_ru",
        "invoices",
        "to_tsvector('russian', coalesce(invoice_number, '') || ' ' || coalesce(notes, ''))",
    ),
]

# Trigram indexes back the fuzzy similarity() path of text_search_condition.
_TRGM_INDEXES = [
    ("ix_parties_name_trgm", "parties", "name"),
    ("ix_invoice_lines_description_trgm", "invoice_lines", "description"),
]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # Trigram indexes below need the pg_trgm extension; on a clean install it is
    # not present yet. pg_trgm is a trusted extension (PG13+), so the DB owner can
    # create it without superuser rights.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, expr in _FTS_INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} USING gin ({expr})"
        )
    for name, table, column in _TRGM_INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} "
            f"ON {table} USING gin ({column} gin_trgm_ops)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for name, _table, _col in _TRGM_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for name, _table, _expr in _FTS_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
