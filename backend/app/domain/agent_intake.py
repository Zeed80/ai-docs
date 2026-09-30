"""Transactional durable-chat intake, independent of HTTP and model execution."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.store import (
    ChatSessionNotFoundError,
    append_chat_attachment,
    append_chat_message,
    ensure_chat_session,
)
from app.db.agent_runtime_models import AgentChannelIdentity, DurableChatRun
from app.db.models import ChatMessage, ChatSession, Document, User, WorkOrder
from app.domain.work_orders import ACTIVE_WORK_STATUSES, create_single_step_plan, create_work_order


class AgentIntakeError(ValueError):
    """A client-visible intake rejection, mapped by the transport adapter."""

    status_code = 409


class IntakeNotFoundError(AgentIntakeError):
    status_code = 404


class IntakeValidationError(AgentIntakeError):
    status_code = 422


class IntakeConflictError(AgentIntakeError):
    status_code = 409


@dataclass(frozen=True)
class VerifiedIntakeIdentity:
    """Identity resolved by an authenticated channel adapter, never request body data."""

    account_key: str
    channel: str


@dataclass(frozen=True)
class IntakeAttachment:
    document_id: uuid.UUID


@dataclass(frozen=True)
class AgentIntakeRequest:
    channel: str
    external_message_id: str
    request_id: uuid.UUID
    content: str
    session_id: uuid.UUID | None = None
    reasoning_mode: Literal["normal", "strict"] = "normal"
    attachments: tuple[IntakeAttachment, ...] = field(default_factory=tuple)
    workspace_context: dict = field(default_factory=dict)
    # A transport may supply a digest of its authenticated raw envelope when
    # preserving an established idempotency contract (HTTP does this).
    input_digest: str | None = None
    # Set only by a trusted Telegram adapter after it resolves the active
    # binding. HTTP deliberately has no channel binding.
    source_binding_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AgentIntakeResult:
    run: DurableChatRun
    order: WorkOrder
    created: bool


def _request_digest(request: AgentIntakeRequest) -> str:
    # Include every field that defines the durable turn.  The namespace itself
    # is looked up separately, so a retry can only return the same input.
    payload = {
        "channel": request.channel,
        "external_message_id": request.external_message_id,
        "request_id": str(request.request_id),
        "content": request.content,
        "session_id": str(request.session_id) if request.session_id else None,
        "reasoning_mode": request.reasoning_mode,
        "attachments": [str(item.document_id) for item in request.attachments],
        "workspace_context": request.workspace_context,
        "source_binding_id": str(request.source_binding_id) if request.source_binding_id else None,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _lock_key(identity: VerifiedIntakeIdentity, request: AgentIntakeRequest) -> int:
    raw = f"{request.channel}:{identity.account_key}:{request.external_message_id}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


def _nonblank_within(value: str, *, label: str, limit: int) -> None:
    if not value.strip() or len(value) > limit:
        raise IntakeValidationError(f"{label} must be non-empty and at most {limit} characters")


async def submit_agent_intake(
    db: AsyncSession,
    *,
    identity: VerifiedIntakeIdentity,
    request: AgentIntakeRequest,
    commit: bool = True,
) -> AgentIntakeResult:
    """Persist exactly one user turn and ready work order; never invoke a model."""
    _nonblank_within(identity.account_key, label="Verified account", limit=200)
    _nonblank_within(identity.channel, label="Verified channel", limit=80)
    _nonblank_within(request.channel, label="Channel", limit=80)
    _nonblank_within(request.external_message_id, label="External message ID", limit=300)
    if request.channel != identity.channel:
        raise IntakeValidationError("Intake channel does not match verified identity")
    # HTTP authentication normally supplies the verified principal while
    # Telegram and cron already resolve it from this table.  If a local owner
    # record exists, a later deactivation must nevertheless close every
    # adapter before it can persist a durable turn.  Missing rows remain the
    # responsibility of the authenticated adapter: legacy test/dev identities
    # and externally provisioned users are not silently converted to a local
    # account here.
    owner_is_active = await db.scalar(
        select(User.is_active).where(User.sub == identity.account_key).with_for_update()
    )
    # Select the scalar value rather than a mapped User: cron may already have
    # loaded the owner earlier in this transaction, and SQLAlchemy's identity
    # map must not let that cached object hide a concurrently committed revoke.
    if owner_is_active is False:
        raise IntakeValidationError("Verified owner is inactive")
    if request.input_digest is not None and request.channel != "http":
        raise IntakeValidationError("Input digest override is reserved for HTTP compatibility")
    if request.channel == "telegram":
        if request.source_binding_id is None:
            raise IntakeValidationError("Telegram intake requires a verified source binding")
        binding = await db.scalar(
            select(AgentChannelIdentity).where(
                AgentChannelIdentity.id == request.source_binding_id,
                AgentChannelIdentity.owner_key == identity.account_key,
                AgentChannelIdentity.channel == "telegram",
                AgentChannelIdentity.is_active.is_(True),
            )
        )
        if binding is None:
            raise IntakeValidationError("Telegram source binding is not verified for the owner")
    elif request.source_binding_id is not None:
        raise IntakeValidationError("Source binding is reserved for Telegram intake")
    if not request.content.strip():
        raise IntakeValidationError("Message must not be empty")
    if len(json.dumps(request.workspace_context).encode()) > 64000:
        raise IntakeValidationError("Workspace context is too large")

    digest = request.input_digest or _request_digest(request)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise IntakeValidationError("Input digest must be a SHA-256 hex digest")
    try:
        # This handles the new-session race as well as duplicate deliveries.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(identity, request)}
        )
        existing = await db.scalar(
            select(DurableChatRun).where(
                DurableChatRun.intake_channel == request.channel,
                DurableChatRun.owner_key == identity.account_key,
                DurableChatRun.external_message_id == request.external_message_id,
            )
        )
        if existing is not None:
            if existing.request_digest != digest:
                raise IntakeConflictError("External message ID already used for different input")
            order = await db.get(WorkOrder, existing.work_order_id)
            if order is None:  # Database integrity should make this unreachable.
                raise IntakeConflictError("Durable chat run is incomplete")
            return AgentIntakeResult(run=existing, order=order, created=False)

        # Reject a forged attachment before creating a new session (and thus
        # before any write which a transport retry could observe).
        documents: list[Document] = []
        for attachment in request.attachments:
            document = await db.scalar(
                select(Document).where(
                    Document.id == attachment.document_id,
                    Document.owner_sub == identity.account_key,
                )
            )
            if document is None:
                raise IntakeNotFoundError("Owned attachment not found")
            documents.append(document)

        try:
            session = await ensure_chat_session(
                db, user_key=identity.account_key, session_id=request.session_id
            )
        except ChatSessionNotFoundError as exc:
            raise IntakeNotFoundError("Chat session not found") from exc
        await db.execute(
            select(ChatSession.id).where(ChatSession.id == session.id).with_for_update()
        )
        durable_session = await db.scalar(
            select(DurableChatRun.id).where(DurableChatRun.session_id == session.id).limit(1)
        )
        if not durable_session and await db.scalar(
            select(ChatMessage.id).where(ChatMessage.session_id == session.id).limit(1)
        ):
            raise IntakeConflictError(
                "Legacy conversation migration is not enabled; create a new conversation"
            )
        active = await db.scalar(
            select(DurableChatRun.id)
            .join(WorkOrder, WorkOrder.id == DurableChatRun.work_order_id)
            .where(
                DurableChatRun.session_id == session.id,
                WorkOrder.status.in_(ACTIVE_WORK_STATUSES),
            )
            .limit(1)
        )
        if active:
            raise IntakeConflictError("A durable turn is already active in this conversation")

        message = await append_chat_message(
            db, session_id=session.id, role="user", content=request.content
        )
        for document in documents:
            await append_chat_attachment(
                db,
                session_id=session.id,
                message_id=message.id,
                document_id=document.id,
                file_name=document.file_name,
                mime_type=document.mime_type,
                size_bytes=document.file_size,
            )
        order = await create_work_order(
            db,
            owner_key=identity.account_key,
            objective=request.content,
            source="durable_chat",
            budgets={"max_replans": 0, "max_wall_clock_seconds": 7200, "max_tool_calls": 200},
            metadata={"chat_session_id": str(session.id)},
            acceptance_criteria=[
                {
                    "criterion_key": "objective_met",
                    "kind": "semantic",
                    "description": request.content,
                    "required": True,
                }
            ],
        )
        run = DurableChatRun(
            owner_key=identity.account_key,
            intake_channel=request.channel,
            external_message_id=request.external_message_id,
            request_id=request.request_id,
            request_digest=digest,
            work_order_id=order.id,
            session_id=session.id,
            user_message_id=message.id,
            source_binding_id=request.source_binding_id,
        )
        db.add(run)
        await create_single_step_plan(
            db,
            order,
            kind="agent_turn",
            title="Durable chat turn",
            input_data={
                "runner": "durable_chat",
                "reasoning_mode": request.reasoning_mode,
                "workspace_context": request.workspace_context,
            },
            max_attempts=1,
            timeout_seconds=7200,
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return AgentIntakeResult(run=run, order=order, created=True)
    except AgentIntakeError:
        # Expected rejections are read-only: attachment, ownership and active
        # turn checks precede the first persistent turn write.  A transport
        # may reuse this session after catching the rejection.
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise IntakeConflictError("External message ID already exists") from exc
    except Exception:
        await db.rollback()
        raise
