"""Persistent identity, delegation and artifacts for agent execution."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, Base, TimestampMixin, UUIDPrimaryKey


class DurableChatRun(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "durable_chat_runs"
    __table_args__ = (
        UniqueConstraint(
            "intake_channel",
            "owner_key",
            "external_message_id",
            name="uq_chat_run_intake_message",
        ),
    )

    owner_key: Mapped[str] = mapped_column(String(200), index=True)
    # The idempotency boundary is owned by a verified channel adapter.  Keep
    # request_id for the existing HTTP response contract, but do not use it as
    # the cross-channel deduplication key.
    intake_channel: Mapped[str] = mapped_column(String(80), nullable=False, default="http")
    external_message_id: Mapped[str] = mapped_column(String(300), nullable=False)
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
    # Telegram replies are bound to the identity which authenticated the
    # inbound update.  It is intentionally nullable for HTTP-originated runs.
    source_binding_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("agent_channel_identities.id", ondelete="RESTRICT"), nullable=True
    )


class ChatLogicalAction(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "chat_logical_actions"

    work_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("work_orders.id"), index=True
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("work_step_attempts.id"))
    call_id: Mapped[str] = mapped_column(Text)
    request: Mapped[dict] = mapped_column(JSON)
    request_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40))
    result: Mapped[dict | None] = mapped_column(JSON)
    result_digest: Mapped[str | None] = mapped_column(String(64))


class ActionReceipt(UUIDPrimaryKey, Base):
    """Immutable proof that one logical action committed one recipient effect."""

    __tablename__ = "action_receipts"
    __table_args__ = (
        UniqueConstraint("logical_action_id", name="uq_action_receipts_logical_action"),
        Index("ix_action_receipts_order_operation", "work_order_id", "operation"),
        Index("ix_action_receipts_owner", "owner_key"),
    )

    logical_action_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("chat_logical_actions.id", ondelete="CASCADE"), nullable=False
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("work_orders.id", ondelete="CASCADE"), nullable=False
    )
    owner_key: Mapped[str] = mapped_column(String(200), nullable=False)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("work_step_attempts.id"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(300), nullable=False)
    artifact_revision: Mapped[str] = mapped_column(String(300), nullable=False)
    receipt_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VerifiedCommitDecision(UUIDPrimaryKey, Base):
    """Immutable owner decision reserving one continuation after a proved commit."""

    __tablename__ = "verified_commit_decisions"
    __table_args__ = (
        UniqueConstraint("source_attempt_id", name="uq_verified_commit_source_attempt"),
        UniqueConstraint("logical_action_id", name="uq_verified_commit_logical_action"),
        UniqueConstraint("target_step_id", name="uq_verified_commit_target_step"),
    )

    work_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("work_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_attempt_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("work_step_attempts.id"), nullable=False
    )
    logical_action_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("chat_logical_actions.id"), nullable=False
    )
    owner_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    approved: Mapped[bool] = mapped_column(nullable=False)
    source_checkpoint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    observation: Mapped[dict] = mapped_column(JSON, nullable=False)
    restored_checkpoint: Mapped[dict | None] = mapped_column(JSON)
    event_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("work_events.id"), nullable=False
    )
    target_step_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("work_steps.id"), nullable=True
    )
    target_revision: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    __table_args__ = (
        Index(
            "uq_active_agent_channel_identity",
            "channel",
            "external_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    owner_key: Mapped[str] = mapped_column(String(200), index=True)
    channel: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)


class TelegramApprovalCallback(UUIDPrimaryKey, Base):
    """One opaque Telegram button bound to one exact human decision."""

    __tablename__ = "telegram_approval_callbacks"
    __table_args__ = (
        UniqueConstraint("token", name="uq_telegram_approval_callback_token"),
        UniqueConstraint(
            "approval_id", "binding_id", name="uq_telegram_approval_callback_approval_binding"
        ),
        Index("ix_telegram_approval_callbacks_approval", "approval_id"),
    )

    # This is deliberately short enough for Telegram callback_data and contains
    # neither the UUID of the approval nor action arguments.
    token: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("approvals.id", ondelete="CASCADE"), nullable=False
    )
    binding_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("agent_channel_identities.id", ondelete="RESTRICT"), nullable=False
    )
    owner_key: Mapped[str] = mapped_column(String(200), nullable=False)
    action_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentOutbox(UUIDPrimaryKey, TimestampMixin, Base):
    """A durable, not-yet-delivered event for one verified channel binding.

    E13 only persists rows.  Claiming leases and performing delivery belong to
    E14, so no worker may interpret these fields as permission to send yet.
    """

    __tablename__ = "agent_outbox"
    __table_args__ = (
        UniqueConstraint(
            "owner_key",
            "destination_binding_id",
            "dedup_key",
            name="uq_agent_outbox_destination_dedup",
        ),
        UniqueConstraint("work_event_id", name="uq_agent_outbox_work_event"),
        Index("ix_agent_outbox_delivery", "delivery_state", "next_attempt_at"),
        Index("ix_agent_outbox_owner", "owner_key"),
    )

    # The originating WorkEvent is durable proof of the domain change.  Both
    # rows are inserted by the producer in the caller's one transaction.
    work_event_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("work_events.id", ondelete="CASCADE"), nullable=False
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("work_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Null for ordinary owner notifications.  A non-null value is the narrow
    # proof that an approval recipient may receive a work-order reference
    # owned by somebody else.
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("approvals.id", ondelete="CASCADE"), nullable=True, index=True
    )
    owner_key: Mapped[str] = mapped_column(String(200), nullable=False)
    destination_binding_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("agent_channel_identities.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    dedup_key: Mapped[str] = mapped_column(String(300), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    lease_token: Mapped[uuid.UUID | None] = mapped_column(GUID())
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentScriptRun(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "agent_script_runs"

    owner_key: Mapped[str] = mapped_column(String(200), index=True)
    work_order_id: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="prepared")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
