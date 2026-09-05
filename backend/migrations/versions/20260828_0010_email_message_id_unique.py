"""Ф1.5: message_id_header uniqueness — dedup must not be a race.

Duplicate detection was SELECT-then-INSERT, which two concurrent polls of the
same mailbox (beat tick + a manual "синхронизировать") walk straight through.
Partial index: messages without a Message-ID header legitimately exist.

Revision ID: 20260828_0010
Revises: 20260828_0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0010"
down_revision: str | None = "20260828_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_email_messages_message_id_header"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    existing = {ix["name"] for ix in sa.inspect(bind).get_indexes("email_messages")}
    if _INDEX in existing:
        return
    # Collapse any duplicates already stored, keeping the earliest row.
    op.execute(
        sa.text(
            """
        DELETE FROM email_messages a
        USING email_messages b
        WHERE a.message_id_header IS NOT NULL
          AND a.message_id_header = b.message_id_header
          AND a.created_at > b.created_at
        """
        )
    )
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX {_INDEX} ON email_messages (message_id_header) "
            "WHERE message_id_header IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX}"))
