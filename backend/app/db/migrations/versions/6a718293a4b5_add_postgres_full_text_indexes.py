"""add postgres full text indexes

Revision ID: 6a718293a4b5
Revises: 5f60718293a4
Create Date: 2026-04-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "6a718293a4b5"
down_revision: Union[str, None] = "5f60718293a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_documents_file_name_fts_ru "
        "ON documents USING gin (to_tsvector('russian', coalesce(file_name, '')))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_text_fts_ru "
        "ON document_chunks USING gin (to_tsvector('russian', coalesce(text, '')))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_extraction_fields_value_fts_ru "
        "ON extraction_fields USING gin "
        "(to_tsvector('russian', coalesce(field_name, '') || ' ' || coalesce(field_value, '')))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_normative_documents_fts_ru "
        "ON normative_documents USING gin "
        "(to_tsvector('russian', coalesce(code, '') || ' ' || coalesce(title, '')))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_normative_requirements_fts_ru "
        "ON normative_requirements USING gin "
        "(to_tsvector('russian', coalesce(requirement_code, '') || ' ' || "
        "coalesce(requirement_type, '') || ' ' || coalesce(text, '')))"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_normative_requirements_fts_ru")
    op.execute("DROP INDEX IF EXISTS ix_normative_documents_fts_ru")
    op.execute("DROP INDEX IF EXISTS ix_extraction_fields_value_fts_ru")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_text_fts_ru")
    op.execute("DROP INDEX IF EXISTS ix_documents_file_name_fts_ru")
