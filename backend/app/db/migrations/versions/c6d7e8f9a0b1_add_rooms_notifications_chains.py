"""add rooms, notifications, approval chains, handover status

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-05-16 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "c6d7e8f9a0b1"
down_revision: Union[str, None] = "b5c6d7e8f9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── rooms ──────────────────────────────────────────────────────────────────
    op.create_table(
        "rooms",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("type", sa.String(length=20), nullable=False, server_default="group"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rooms_type"), "rooms", ["type"])

    # ── room_members ───────────────────────────────────────────────────────────
    op.create_table(
        "room_members",
        sa.Column("room_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("user_sub", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="member"),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("room_id", "user_sub"),
    )
    op.create_index("ix_room_members_user_sub", "room_members", ["user_sub"])

    # ── room_messages ──────────────────────────────────────────────────────────
    op.create_table(
        "room_messages",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("sender_sub", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_type", sa.String(length=20), nullable=False, server_default="text"),
        sa.Column("reply_to_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("is_edited", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reply_to_id"], ["room_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_room_messages_room_id", "room_messages", ["room_id"])
    op.create_index("ix_room_messages_created_at", "room_messages", ["created_at"])

    # ── room_message_attachments ───────────────────────────────────────────────
    op.create_table(
        "room_message_attachments",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("file_name", sa.String(length=500), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("thumbnail_key", sa.String(length=1000), nullable=True),
        sa.Column("ingest_job_id", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["room_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_room_message_attachments_message_id", "room_message_attachments", ["message_id"])

    # ── notifications ──────────────────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("user_sub", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=True),
        sa.Column("entity_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("action_url", sa.String(length=500), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_user_sub", "notifications", ["user_sub"])
    op.create_index("ix_notifications_is_read", "notifications", ["is_read"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])

    # ── approvals: add chain fields ────────────────────────────────────────────
    op.add_column("approvals", sa.Column("chain_order", sa.Integer(), nullable=True))
    op.add_column("approvals", sa.Column("chain_root_id", PG_UUID(as_uuid=True), nullable=True))
    op.add_column("approvals", sa.Column("requires_all", sa.Boolean(), nullable=False, server_default="false"))

    # ── handovers: add status field ────────────────────────────────────────────
    op.add_column("handovers", sa.Column(
        "status", sa.String(length=20), nullable=False, server_default="pending"
    ))
    op.create_index("ix_handovers_status", "handovers", ["status"])


def downgrade() -> None:
    op.drop_index("ix_handovers_status", table_name="handovers")
    op.drop_column("handovers", "status")

    op.drop_column("approvals", "requires_all")
    op.drop_column("approvals", "chain_root_id")
    op.drop_column("approvals", "chain_order")

    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_is_read", table_name="notifications")
    op.drop_index("ix_notifications_user_sub", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_room_message_attachments_message_id", table_name="room_message_attachments")
    op.drop_table("room_message_attachments")

    op.drop_index("ix_room_messages_created_at", table_name="room_messages")
    op.drop_index("ix_room_messages_room_id", table_name="room_messages")
    op.drop_table("room_messages")

    op.drop_index("ix_room_members_user_sub", table_name="room_members")
    op.drop_table("room_members")

    op.drop_index(op.f("ix_rooms_type"), table_name="rooms")
    op.drop_table("rooms")
