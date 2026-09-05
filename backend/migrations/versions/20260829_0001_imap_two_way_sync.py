"""Ф2.1/2.2: per-folder sync state, message UIDs, and a write-back queue.

Revision ID: 20260829_0001
Revises: 20260828_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "20260829_0001"
down_revision: str | None = "20260828_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    cols = {c["name"] for c in insp.get_columns("email_messages")}
    if "imap_uid" not in cols:
        op.add_column("email_messages", sa.Column("imap_uid", sa.BigInteger(), nullable=True))
        op.create_index("ix_email_messages_imap_uid", "email_messages", ["imap_uid"])
    if "imap_folder" not in cols:
        op.add_column("email_messages", sa.Column("imap_folder", sa.String(500), nullable=True))
    if "flags_synced_at" not in cols:
        op.add_column(
            "email_messages",
            sa.Column("flags_synced_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not insp.has_table("mailbox_folders"):
        op.create_table(
            "mailbox_folders",
            sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
            sa.Column("mailbox", sa.String(255), nullable=False),
            sa.Column("remote_name", sa.String(500), nullable=False),
            sa.Column("local_folder", sa.String(30), nullable=True),
            sa.Column("special_use", sa.String(40), nullable=True),
            sa.Column("uid_validity", sa.BigInteger(), nullable=True),
            sa.Column("uid_next", sa.BigInteger(), nullable=True),
            sa.Column("last_seen_uid", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("highest_modseq", sa.BigInteger(), nullable=True),
            sa.Column("is_selectable", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sync_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sync_error", sa.Text(), nullable=True),
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
            sa.UniqueConstraint("mailbox", "remote_name", name="uq_mailbox_folder"),
        )
        op.create_index("ix_mailbox_folders_mailbox", "mailbox_folders", ["mailbox"])
        op.create_index("ix_mailbox_folders_local_folder", "mailbox_folders", ["local_folder"])

    if not insp.has_table("email_sync_ops"):
        op.create_table(
            "email_sync_ops",
            sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
            sa.Column("message_id", PG_UUID(as_uuid=True), nullable=True),
            sa.Column("mailbox", sa.String(255), nullable=False),
            sa.Column("op", sa.String(20), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("state", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.PrimaryKeyConstraint("id"),
        )
        for col in ("message_id", "mailbox", "state"):
            op.create_index(f"ix_email_sync_ops_{col}", "email_sync_ops", [col])

    # Existing rows were all fetched from the mailbox's single configured folder.
    op.execute(
        sa.text(
            "UPDATE email_messages m SET imap_folder = c.imap_folder "
            "FROM mailbox_configs c WHERE c.name = m.mailbox AND m.imap_folder IS NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table in ("email_sync_ops", "mailbox_folders"):
        if insp.has_table(table):
            op.drop_table(table)
    cols = {c["name"] for c in insp.get_columns("email_messages")}
    for name in ("flags_synced_at", "imap_folder", "imap_uid"):
        if name in cols:
            op.drop_column("email_messages", name)
