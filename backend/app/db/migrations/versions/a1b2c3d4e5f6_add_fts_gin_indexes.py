"""add GIN tsvector indexes for evidence_spans and knowledge_nodes FTS

Revision ID: a1b2c3d4e5f6
Revises: 718293a4b5c6
Create Date: 2026-05-01 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "718293a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # evidence_spans — not covered by previous FTS migration
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_evidence_spans_text_fts_ru "
        "ON evidence_spans USING gin (to_tsvector('russian', coalesce(text, '')))"
    )
    # knowledge_nodes — title + summary
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_nodes_fts_ru "
        "ON knowledge_nodes USING gin ("
        "  to_tsvector('russian', coalesce(title, ''))"
        "  || to_tsvector('russian', coalesce(summary, ''))"
        ")"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_knowledge_nodes_fts_ru")
    op.execute("DROP INDEX IF EXISTS ix_evidence_spans_text_fts_ru")
