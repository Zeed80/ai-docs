"""Ф8: search e-mail by what its attachments turned out to CONTAIN.

The roadmap assumed the recognised text sits on ``documents`` — it does not.
``domain/models.py`` declares a legacy ``Document`` with ``extracted_text``
mapped to the same table name, but the live ``documents`` table has no such
column; the recognised content is the JSON in ``document_extractions``. So the
search goes there, and this index makes the cast searchable instead of scanned.

Revision ID: 20260829_0003
Revises: 20260829_0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_0003"
down_revision: str | None = "20260829_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_extractions_structured_trgm "
        "ON document_extractions USING gin ((structured_data::text) gin_trgm_ops)"
    )
    # The join that gets from a matching extraction back to the letter.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_documents_source_email "
        "ON documents (source_email_id) WHERE source_email_id IS NOT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_document_extractions_structured_trgm")
    op.execute("DROP INDEX IF EXISTS ix_documents_source_email")
