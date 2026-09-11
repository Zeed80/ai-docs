"""Persistent identity, delegation and artifacts for agent execution."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, Base, TimestampMixin, UUIDPrimaryKey


class DurableChatRun(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "durable_chat_runs"
    __table_args__ = (UniqueConstraint("owner_key", "request_id", name="uq_chat_run_request"),)

    owner_key: Mapped[str] = mapped_column(String(200), index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(GUID())
    request_digest: Mapped[str] = mapped_column(String(64))
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("work_orders.id"), unique=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("chat_sessions.id"), index=True
    )
    user_message_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("chat_messages.id"))
    result_message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("chat_messages.id")
    )


class OwnedWorkspaceBlock(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "owned_workspace_blocks"
    __table_args__ = (UniqueConstraint("owner_key", "block_key"),)

    owner_key: Mapped[str] = mapped_column(String(200), index=True)
    block_key: Mapped[str] = mapped_column(String(300))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class DelegationGrant(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "agent_delegation_grants"

    owner_key: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(300))
    actions: Mapped[list] = mapped_column(JSON)
    constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    max_actions: Mapped[int] = mapped_column(Integer, default=200)
    used_actions: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentChannelIdentity(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "agent_channel_identities"
    __table_args__ = (UniqueConstraint("channel", "external_id"),)

    owner_key: Mapped[str] = mapped_column(String(200), index=True)
    channel: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(200))


class AgentScriptRun(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "agent_script_runs"

    owner_key: Mapped[str] = mapped_column(String(200), index=True)
    work_order_id: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="prepared")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
