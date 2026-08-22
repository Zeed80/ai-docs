"""full-text, trigram and filter indexes for catalog positions

The catalog had no text index at all: search was "vector OR ILIKE" over
thousands of rows, and a typo found nothing.

The FTS expression must match, byte for byte, what
`app/db/text_search.py::immutable_fts_expression` generates for these columns —
otherwise PostgreSQL cannot use the index and silently falls back to a seq scan.
(`concat_ws`, used by the older text_search helpers, is only STABLE and cannot
appear in an index expression at all.)

Revision ID: 20260822_0002
Revises: 20260822_0001
Create Date: 2026-08-22
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260822_0002"
down_revision: Union[str, None] = "20260822_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Byte-for-byte the expression app/db/text_search.py::immutable_fts_expression
# generates for these columns — the planner uses the index only on an exact
# match, and plain `||` is the immutable form PostgreSQL accepts in an index.
_FTS_EXPRESSION = (
    "to_tsvector('russian', (((coalesce(tool_catalog_entries.name, '') || ' ') || coalesce(tool_catalog_entries.part_number, '')) || ' ') || coalesce(tool_catalog_entries.description, ''))"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_tool_catalog_entries_fts_ru "
        f"ON tool_catalog_entries USING gin ({_FTS_EXPRESSION})"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_catalog_entries_name_trgm "
        "ON tool_catalog_entries USING gin (name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_catalog_entries_part_number_trgm "
        "ON tool_catalog_entries USING gin (part_number gin_trgm_ops)"
    )
    # The filter combinations the browser and the search actually use.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_catalog_entries_supplier_active "
        "ON tool_catalog_entries (supplier_id, is_active)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_catalog_entries_document_active "
        "ON tool_catalog_entries (source_document_id, is_active)"
    )

    # Identity of an active position inside its catalog. Existing duplicates
    # would break the index, so retire all but the newest of each group first —
    # a failed migration mid-deploy costs more than a deactivated row.
    op.execute(
        """
        UPDATE tool_catalog_entries e
           SET is_active = false
         WHERE e.is_active
           AND e.content_hash IS NOT NULL
           AND EXISTS (
                 SELECT 1 FROM tool_catalog_entries other
                  WHERE other.is_active
                    AND other.content_hash = e.content_hash
                    AND other.source_document_id IS NOT DISTINCT FROM e.source_document_id
                    AND (other.created_at, other.id) > (e.created_at, e.id)
           )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_catalog_entries_active_content "
        "ON tool_catalog_entries (source_document_id, content_hash) "
        "WHERE is_active AND content_hash IS NOT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name in (
        "uq_tool_catalog_entries_active_content",
        "ix_tool_catalog_entries_document_active",
        "ix_tool_catalog_entries_supplier_active",
        "ix_tool_catalog_entries_part_number_trgm",
        "ix_tool_catalog_entries_name_trgm",
        "ix_tool_catalog_entries_fts_ru",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name}")
