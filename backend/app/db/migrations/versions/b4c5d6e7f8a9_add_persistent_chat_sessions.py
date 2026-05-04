"""add persistent chat sessions and messages

Revision ID: b4c5d6e7f8a9
Revises: a1b2c3d4e5f6
Create Date: 2026-05-04 18:25:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("user_key", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
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
    op.create_index(op.f("ix_chat_sessions_user_key"), "chat_sessions", ["user_key"], unique=False)
    op.create_index(
        op.f("ix_chat_sessions_last_message_at"),
        "chat_sessions",
        ["last_message_at"],
        unique=False,
    )
    op.create_index(op.f("ix_chat_sessions_deleted_at"), "chat_sessions", ["deleted_at"], unique=False)

    op.create_table(
        "chat_messages",
        sa.Column("session_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
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
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_chat_messages_session_id"), "chat_messages", ["session_id"], unique=False)
    op.create_index(op.f("ix_chat_messages_role"), "chat_messages", ["role"], unique=False)

    op.create_table(
        "chat_message_attachments",
        sa.Column("session_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("file_name", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
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
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["chat_messages.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_chat_message_attachments_session_id"),
        "chat_message_attachments",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_message_attachments_message_id"),
        "chat_message_attachments",
        ["message_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_message_attachments_document_id"),
        "chat_message_attachments",
        ["document_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_chat_message_attachments_document_id"), table_name="chat_message_attachments")
    op.drop_index(op.f("ix_chat_message_attachments_message_id"), table_name="chat_message_attachments")
    op.drop_index(op.f("ix_chat_message_attachments_session_id"), table_name="chat_message_attachments")
    op.drop_table("chat_message_attachments")
    op.drop_index(op.f("ix_chat_messages_role"), table_name="chat_messages")
    op.drop_index(op.f("ix_chat_messages_session_id"), table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index(op.f("ix_chat_sessions_deleted_at"), table_name="chat_sessions")
    op.drop_index(op.f("ix_chat_sessions_last_message_at"), table_name="chat_sessions")
    op.drop_index(op.f("ix_chat_sessions_user_key"), table_name="chat_sessions")
    op.drop_table("chat_sessions")
