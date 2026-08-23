"""searchable text of catalog pages

The parser already read every page's text (text layer or OCR) and threw it
away, keeping only its length — so "найди это в PDF" had nothing to search.
Storing it makes the page itself findable, which is what a person expects when
looking at a 948-page catalog: type a word, land on the page.

Revision ID: 20260823_0001
Revises: 20260822_0002
Create Date: 2026-08-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260823_0001"
down_revision: Union[str, None] = "20260822_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Byte-for-byte what app/db/text_search.py::immutable_fts_expression produces
# for this column — the planner uses the index only on an exact match, and a
# mismatch degrades silently to a seq scan over every page of every catalog.
_FTS_EXPRESSION = "to_tsvector('russian', coalesce(catalog_pages.text, ''))"


def upgrade() -> None:
    op.add_column("catalog_pages", sa.Column("text", sa.Text(), nullable=True))

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_catalog_pages_fts_ru "
        f"ON catalog_pages USING gin ({_FTS_EXPRESSION})"
    )
    # An article code is not a word to a text-search dictionary ("11V9-100x3x10"
    # tokenizes into pieces), so exact-ish substring matching needs trigrams.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_catalog_pages_text_trgm "
        "ON catalog_pages USING gin (text gin_trgm_ops)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_catalog_pages_text_trgm")
        op.execute("DROP INDEX IF EXISTS ix_catalog_pages_fts_ru")
    op.drop_column("catalog_pages", "text")
