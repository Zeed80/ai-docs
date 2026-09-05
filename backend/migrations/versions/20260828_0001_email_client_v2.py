"""email client v2: read/star/folder state, labels, normalised attachments, FTS

Turns the placeholder mail view into a real client:
  * per-thread / per-message read, star, folder (app-level — shared mailboxes
    are \\Seen-flagged by triage on ingest, so IMAP can't tell us "unread");
  * Gmail-style labels (app-level, not IMAP folders);
  * attachments as rows (raw-byte access for the agent, files on outbound mail);
  * a Russian full-text index for search.

Revision ID: 20260828_0001
Revises: 20260826_0002
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "20260828_0001"
down_revision: str | None = "20260826_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_THREAD_COLS = [
    sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("is_starred", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("has_attachments", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("folder", sa.String(20), nullable=False, server_default="inbox"),
    sa.Column("last_snippet", sa.String(300), nullable=True),
    sa.Column("unread_count", sa.Integer(), nullable=False, server_default="0"),
]

_MESSAGE_COLS = [
    sa.Column("references", sa.String(2000), nullable=True),
    sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("is_starred", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("folder", sa.String(20), nullable=False, server_default="inbox"),
    sa.Column("snippet", sa.String(300), nullable=True),
    sa.Column("body_html_sanitized", sa.Text(), nullable=True),
]


def upgrade() -> None:
    for col in _THREAD_COLS:
        op.add_column("email_threads", col)
    for col in _MESSAGE_COLS:
        op.add_column("email_messages", col)

    op.create_index("ix_email_threads_folder", "email_threads", ["folder"])
    op.create_index("ix_email_messages_is_read", "email_messages", ["is_read"])

    op.create_table(
        "email_labels",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("color", sa.String(20), nullable=True),
        sa.Column("mailbox", sa.String(100), nullable=True),
        sa.Column("owner_sub", sa.String(255), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_labels_mailbox", "email_labels", ["mailbox"])
    op.create_index("ix_email_labels_owner_sub", "email_labels", ["owner_sub"])

    op.create_table(
        "email_thread_labels",
        sa.Column("thread_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("label_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("added_by", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["thread_id"], ["email_threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["label_id"], ["email_labels.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("thread_id", "label_id"),
    )
    op.create_index("ix_email_thread_labels_thread_id", "email_thread_labels", ["thread_id"])
    op.create_index("ix_email_thread_labels_label_id", "email_thread_labels", ["label_id"])

    op.create_table(
        "email_attachments",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(200), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("content_id", sa.String(200), nullable=True),
        sa.Column("is_inline", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("storage_path", sa.String(1000), nullable=True),
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["message_id"], ["email_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_attachments_message_id", "email_attachments", ["message_id"])
    op.create_index("ix_email_attachments_document_id", "email_attachments", ["document_id"])
    op.create_index("ix_email_attachments_sha256", "email_attachments", ["sha256"])

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Russian FTS over subject + body for /api/email/search.
        op.execute(
            sa.text(
                "CREATE INDEX ix_email_messages_fts ON email_messages "
                "USING GIN (to_tsvector('russian', "
                "coalesce(subject,'') || ' ' || coalesce(body_text,'')))"
            )
        )
        # Backfill has_attachments / folder on existing rows.
        op.execute(
            sa.text(
                "UPDATE email_messages SET folder = CASE WHEN is_inbound THEN 'inbox' ELSE 'sent' END"
            )
        )
        op.execute(
            sa.text(
                "UPDATE email_threads t SET has_attachments = EXISTS ("
                "SELECT 1 FROM email_messages m WHERE m.thread_id = t.id AND m.has_attachments)"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP INDEX IF EXISTS ix_email_messages_fts"))

    op.drop_table("email_attachments")
    op.drop_table("email_thread_labels")
    op.drop_table("email_labels")

    op.drop_index("ix_email_messages_is_read", "email_messages")
    op.drop_index("ix_email_threads_folder", "email_threads")
    for col in reversed(_MESSAGE_COLS):
        op.drop_column("email_messages", col.name)
    for col in reversed(_THREAD_COLS):
        op.drop_column("email_threads", col.name)
