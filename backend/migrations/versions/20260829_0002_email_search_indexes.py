"""Ф8: make e-mail search survive a real mailbox.

The comment at api/email.py claimed "trigram-accelerated ILIKE" while no
trigram index on email_messages existed in any migration — every search with a
non-empty query did a sequential scan over body_text, and the OR against the
FTS branch kept the planner from using the GIN index either.

Also indexes the fields the client actually filters on but the schema never
supported: attachment filenames, and the received_at/folder pair the thread
list orders by.

Revision ID: 20260829_0002
Revises: 20260829_0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_0002"
down_revision: str | None = "20260829_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    # Substring search over subject/body — the ILIKE branch of /api/email/search.
    ("ix_email_messages_subject_trgm", "email_messages", "subject"),
    ("ix_email_messages_body_trgm", "email_messages", "body_text"),
    ("ix_email_messages_from_trgm", "email_messages", "from_address"),
    # "Find the letter that had счёт-2562.pdf attached" had no index at all.
    ("ix_email_attachments_filename_trgm", "email_attachments", "filename"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, column in _INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} USING gin ({column} gin_trgm_ops)"
        )
    # The thread list is ordered by (last_message_at, id) inside a folder;
    # keyset pagination without this walks the whole table per page.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_email_threads_folder_last_message "
        "ON email_threads (folder, last_message_at DESC NULLS LAST, id DESC)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name, _table, _column in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute("DROP INDEX IF EXISTS ix_email_threads_folder_last_message")
