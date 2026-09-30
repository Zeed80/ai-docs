"""Pilot durable chat transport. No task is owned by the HTTP connection."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.auth.jwt import get_current_user, is_service_account
from app.auth.models import UserInfo
from app.chat.store import create_chat_session
from app.db.agent_runtime_models import (
    ArchivedConversationImport,
    ChatLogicalAction,
    DurableChatRun,
    VerifiedCommitDecision,
)
from app.db.models import (
    ChatMessage,
    ChatMessageAttachment,
    ChatSession,
    Document,
    User,
    WorkEvent,
    WorkOrder,
    WorkPlan,
    WorkStep,
    WorkStepAttempt,
)
from app.db.session import get_db
from app.domain.agent_intake import (
    AgentIntakeError,
    AgentIntakeRequest,
    IntakeAttachment,
    VerifiedIntakeIdentity,
    submit_agent_intake,
)
from app.domain.work_orders import append_event, create_single_step_plan

router = APIRouter(prefix="/api/agent/chat-runs", tags=["durable-chat"])


class ChatRunAttachment(BaseModel):
    document_id: uuid.UUID
    file_name: str = ""
    mime_type: str | None = None
    size_bytes: int | None = None


class ChatRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    session_id: uuid.UUID | None = None
    content: str = Field(min_length=1, max_length=12000)
    reasoning_mode: Literal["normal", "strict"] = "normal"
    attachments: list[ChatRunAttachment] = Field(default_factory=list, max_length=20)
    workspace_context: dict = Field(default_factory=dict)


class ArchivedConversationImportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    message_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    title: str = Field(default="Продолжение архивного чата", max_length=500)

    @model_validator(mode="after")
    def unique_selection(self):
        if len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("message_ids must be unique")
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("attachment_ids must be unique")
        return self


def _archive_import_digest(source_session_id: uuid.UUID, body: ArchivedConversationImportCreate):
    payload = {
        "source_session_id": str(source_session_id),
        "message_ids": sorted(str(item) for item in body.message_ids),
        "attachment_ids": sorted(str(item) for item in body.attachment_ids),
        "title": " ".join(body.title.split()) or "Продолжение архивного чата",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


@router.post("/archive-imports/{source_session_id}", status_code=201)
async def import_archived_conversation(
    source_session_id: uuid.UUID,
    body: ArchivedConversationImportCreate,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    """Create a new conversation with inert, explicitly selected archive context."""
    if is_service_account(user):
        raise HTTPException(403, "Archive import requires a human owner")
    request_digest = _archive_import_digest(source_session_id, body)
    lock_raw = f"archive-import:{user.sub}:{body.request_id}".encode()
    lock_key = int.from_bytes(hashlib.sha256(lock_raw).digest()[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    existing = await db.scalar(
        select(ArchivedConversationImport).where(
            ArchivedConversationImport.owner_key == user.sub,
            ArchivedConversationImport.request_id == body.request_id,
        )
    )
    if existing is not None:
        if existing.request_digest != request_digest:
            raise HTTPException(409, "Archive import request already used differently")
        return {
            "id": existing.id,
            "source_session_id": existing.source_session_id,
            "target_session_id": existing.target_session_id,
            "record_count": len(existing.records),
            "created": False,
        }

    source = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == source_session_id,
            ChatSession.user_key == user.sub,
            ChatSession.deleted_at.is_(None),
        )
    )
    if source is None:
        raise HTTPException(404, "Archived chat session not found")
    # A durable source is not an archive and must be continued through its own
    # run lifecycle instead of being copied into another execution context.
    if await db.scalar(
        select(DurableChatRun.id).where(DurableChatRun.session_id == source.id).limit(1)
    ):
        raise HTTPException(409, "Only an archived conversation can be imported")

    messages = list(
        await db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.session_id == source.id,
                ChatMessage.id.in_(body.message_ids),
            )
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
    )
    if len(messages) != len(body.message_ids):
        raise HTTPException(404, "Selected archived message not found")
    messages_by_id = {message.id: message for message in messages}
    messages = [messages_by_id[message_id] for message_id in body.message_ids]
    if any(message.role not in {"user", "assistant"} for message in messages):
        raise HTTPException(422, "Only archived user and assistant text can be imported")

    attachments: list[ChatMessageAttachment] = []
    if body.attachment_ids:
        attachments = list(
            await db.scalars(
                select(ChatMessageAttachment).where(
                    ChatMessageAttachment.id.in_(body.attachment_ids),
                    ChatMessageAttachment.session_id == source.id,
                    ChatMessageAttachment.message_id.in_(body.message_ids),
                )
            )
        )
        if len(attachments) != len(body.attachment_ids):
            raise HTTPException(404, "Selected archived attachment not found")
        if any(item.document_id is None for item in attachments):
            raise HTTPException(404, "Selected archived attachment is no longer readable")
        document_ids = {item.document_id for item in attachments}
        owned_document_ids = set(
            await db.scalars(
                select(Document.id).where(
                    Document.id.in_(document_ids),
                    Document.owner_sub == user.sub,
                )
            )
        )
        if owned_document_ids != document_ids:
            # A historical link is not proof of current access.
            raise HTTPException(404, "Selected archived attachment is no longer readable")

    attachments_by_message: dict[uuid.UUID, list[ChatMessageAttachment]] = {}
    for attachment in attachments:
        attachments_by_message.setdefault(attachment.message_id, []).append(attachment)
    records = [
        {
            "source_message_id": str(message.id),
            "source_role": message.role,
            "content": message.content or "",
            "created_at": message.created_at.isoformat(),
            "attachments": [
                {
                    "attachment_id": str(item.id),
                    "document_id": str(item.document_id),
                    "file_name": item.file_name,
                    "mime_type": item.mime_type,
                    "size_bytes": item.size_bytes,
                }
                for item in sorted(
                    attachments_by_message.get(message.id, []), key=lambda value: str(value.id)
                )
            ],
            "executable": False,
        }
        for message in messages
    ]
    if len(json.dumps(records, ensure_ascii=False).encode()) > 48_000:
        raise HTTPException(422, "Selected archived context is too large")

    target = await create_chat_session(
        db,
        user_key=user.sub,
        title=" ".join(body.title.split()) or "Продолжение архивного чата",
    )
    imported = ArchivedConversationImport(
        owner_key=user.sub,
        request_id=body.request_id,
        request_digest=request_digest,
        source_session_id=source.id,
        target_session_id=target.id,
        records=records,
        attachment_ids=[str(item) for item in body.attachment_ids],
        created_at=datetime.now(UTC),
    )
    db.add(imported)
    await db.commit()
    return {
        "id": imported.id,
        "source_session_id": imported.source_session_id,
        "target_session_id": imported.target_session_id,
        "record_count": len(records),
        "created": True,
    }


class ChatResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: uuid.UUID
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    approved: bool = Field(strict=True)
    intent: Literal["confirmation", "verified_commit"] = "confirmation"
    action_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def require_verified_action(self):
        if self.intent == "verified_commit" and self.action_id is None:
            raise ValueError("verified_commit requires action_id")
        if self.intent == "confirmation" and self.action_id is not None:
            raise ValueError("action_id is only valid for verified_commit")
        return self


async def existing_decision(db, order_id, attempt_id):
    return await db.scalar(
        select(WorkEvent)
        .where(
            WorkEvent.work_order_id == order_id,
            WorkEvent.event_type == "chat.continuation_decided",
            WorkEvent.payload["attempt_id"].as_string() == str(attempt_id),
        )
        .limit(1)
    )


async def _verified_commit_owner_verifier(db, order):
    """Build verifier authority from persisted owner state, never JWT claims."""
    from app.auth.models import UserInfo, UserRole

    owner = await db.scalar(
        select(User).where(
            User.sub == order.owner_key,
            User.is_active.is_(True),
        )
    )
    if owner is None:
        raise ValueError("Verified-commit owner is inactive or unavailable")
    try:
        owner_role = UserRole(owner.role)
    except ValueError:
        raise ValueError("Verified-commit owner role is invalid") from None
    return UserInfo(
        sub=owner.sub,
        email=owner.email,
        name=owner.name,
        preferred_username=owner.preferred_username,
        roles=[owner_role],
        department_id=str(owner.department_id) if owner.department_id else None,
        section_access=owner.section_access,
        timezone=owner.timezone,
        via_agent=True,
    )


@router.post("/{run_id}/resume", status_code=202)
async def resume_chat_run(
    run_id: uuid.UUID,
    body: ChatResumeRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    from app.ai.agent_config import get_builtin_agent_config
    from app.domain.chat_continuation import (
        confirmation_state,
        validate_current_config,
        validate_wall_budget,
    )

    if is_service_account(user):
        raise HTTPException(403, "Continuation requires a human owner")
    run = await owned_run(db, run_id, user)
    await db.execute(
        select(ChatSession.id).where(ChatSession.id == run.session_id).with_for_update()
    )
    # Same lock as cancellation and intake settlement; duplicate clicks cannot
    # allocate two revisions or decide the same snapshot twice.
    order = await db.get(WorkOrder, run.work_order_id, with_for_update=True)
    if body.intent == "verified_commit":
        return await _verified_commit_resume(db, run, order, body, user)
    old = await existing_decision(db, order.id, body.attempt_id)
    if old:
        if old.payload["sha256"] != body.sha256 or old.payload["approved"] != body.approved:
            raise HTTPException(409, "Checkpoint already decided differently")
        return describe(run, order)
    if order.status != "blocked" or run.result_message_id is not None:
        raise HTTPException(409, "Chat is not waiting at a confirmation boundary")
    # Intake locks the session before creating another turn. Do not revise an
    # earlier turn after the user has moved this conversation forward.
    latest = await db.scalar(
        select(DurableChatRun.id)
        .where(DurableChatRun.session_id == run.session_id)
        .order_by(DurableChatRun.created_at.desc(), DurableChatRun.id.desc())
        .limit(1)
    )
    if latest != run.id:
        raise HTTPException(409, "A newer conversation turn exists")
    attempt = await db.get(WorkStepAttempt, body.attempt_id)
    step = await db.get(WorkStep, attempt.step_id) if attempt else None
    if attempt is None or step is None:
        raise HTTPException(409, "Checkpoint attempt not found")
    record = attempt.checkpoint or {}
    try:
        payload = confirmation_state(record, order, step, attempt)
        if record["snapshot"]["sha256"] != body.sha256:
            raise ValueError("Checkpoint digest changed")
        if body.approved:
            validate_current_config(payload, get_builtin_agent_config())
            validate_wall_budget(order)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    decision = {
        "attempt_id": str(attempt.id),
        "sha256": body.sha256,
        "approved": body.approved,
        "source_revision": order.plan_revision,
        "call_id": payload["pending_calls"][0]["id"],
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    event = await append_event(
        db, order.id, "chat.continuation_decided", actor=user.sub, payload=decision
    )
    if body.approved:
        _, continuation = await create_single_step_plan(
            db,
            order,
            kind="agent_turn",
            title="Continue confirmed chat action",
            input_data={
                "runner": "durable_chat",
                "continuation_event_id": str(event.id),
                "workspace_context": (step.input_ or {}).get("workspace_context", {}),
            },
            max_attempts=1,
            timeout_seconds=7200,
            actor=user.sub,
        )
        event.payload = {
            **decision,
            "target_step_id": str(continuation.id),
            "target_revision": order.plan_revision,
        }
        order.blocker = None
    await db.commit()
    return describe(run, order)


async def _verified_commit_resume(db, run, order, body, user):
    """Reserve one E11.1 continuation step; the E11.2 worker consumes it later."""
    from app.ai.agent_config import get_builtin_agent_config
    from app.ai.chat_checkpoint import unpack_checkpoint
    from app.ai.tool_result import normalize_http_one_db_commit_response
    from app.domain.action_receipts import read_receipt
    from app.domain.artifact_verification import validate_artifact_verdict, verify_action_artifact
    from app.domain.chat_action_journal import digest
    from app.domain.chat_continuation import (
        config_fingerprint,
        plan_fingerprint,
        validate_shared_budgets,
        validate_wall_budget,
        verified_commit_continuation_state,
    )

    request_digest = digest(body.model_dump(mode="json"))
    old = await db.scalar(
        select(VerifiedCommitDecision).where(
            VerifiedCommitDecision.source_attempt_id == body.attempt_id,
            VerifiedCommitDecision.work_order_id == order.id,
            VerifiedCommitDecision.owner_key == user.sub,
        )
    )
    if old is not None:
        if old.request_digest != request_digest:
            raise HTTPException(409, "Verified-commit checkpoint already decided differently")
        return describe(run, order)
    if (
        order.status != "blocked"
        or order.canceled_at is not None
        or run.result_message_id is not None
    ):
        raise HTTPException(409, "Chat is not at a verified-commit boundary")
    latest = await db.scalar(
        select(DurableChatRun.id)
        .where(DurableChatRun.session_id == run.session_id)
        .order_by(DurableChatRun.created_at.desc(), DurableChatRun.id.desc())
        .limit(1)
    )
    if latest != run.id:
        raise HTTPException(409, "A newer conversation turn exists")
    attempt = await db.get(WorkStepAttempt, body.attempt_id)
    step = await db.get(WorkStep, attempt.step_id) if attempt else None
    action = await db.get(ChatLogicalAction, body.action_id)
    if (
        attempt is None
        or step is None
        or action is None
        or step.work_order_id != order.id
        or action.work_order_id != order.id
        or action.attempt_id != attempt.id
    ):
        raise HTTPException(409, "Verified-commit source not found")
    record = attempt.checkpoint or {}
    if (
        record.get("kind") != "durable_chat"
        or record.get("owner_key") != order.owner_key
        or record.get("work_order_id") != str(order.id)
        or record.get("step_id") != str(step.id)
        or record.get("attempt_id") != str(attempt.id)
        or record.get("plan_id") != str(step.plan_id)
        or record.get("plan_revision") != order.plan_revision
    ):
        raise HTTPException(409, "Verified-commit checkpoint binding changed")
    if (record.get("snapshot") or {}).get("sha256") != body.sha256:
        raise HTTPException(409, "Checkpoint digest changed")
    if digest(action.request) != action.request_digest:
        raise HTTPException(409, "Logical action integrity mismatch")
    receipt = await read_receipt(db, action)
    if receipt is None:
        raise HTTPException(409, "Committed recipient receipt is missing")
    try:
        verifier = await _verified_commit_owner_verifier(db, order)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    observation = await verify_action_artifact(db, action=action, user=verifier)
    if not validate_artifact_verdict(observation) or observation.get("status") != "matched":
        raise HTTPException(409, "Current recipient artifact is not proved")
    adapted = normalize_http_one_db_commit_response(
        receipt["response"], operation=receipt["operation"]
    ).model_dump(mode="json")
    if (
        digest(receipt["response"]) != receipt["response_digest"]
        or adapted.get("status") != "succeeded"
        or adapted.get("data") != receipt["response"]
    ):
        raise HTTPException(409, "Recipient response adaptation binding changed")
    checkpoint_payload = unpack_checkpoint(record["snapshot"])
    phase = checkpoint_payload.get("phase")
    completed = checkpoint_payload.get("completed_call") or {}
    if phase == "tool_started":
        action_valid = (
            action.status == "started" and action.result is None and action.result_digest is None
        )
    elif phase == "tool_recorded":
        action_valid = (
            action.status == "outcome_unknown"
            and completed.get("action_id") == str(action.id)
            and action.result == completed.get("result")
            and action.result_digest == digest(action.result)
        )
    else:
        action_valid = False
    if not action_valid:
        raise HTTPException(409, "Logical action frontier binding changed")
    plan = await db.get(WorkPlan, step.plan_id)
    if plan is None or plan.work_order_id != order.id or plan.revision != record["plan_revision"]:
        raise HTTPException(409, "Source plan binding changed")
    plan_digest = plan_fingerprint(plan)
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=30)
    try:
        validate_wall_budget(order)
        await validate_shared_budgets(db, order)
        restored = verified_commit_continuation_state(
            checkpoint=record["snapshot"],
            source={
                "order_status": order.status,
                "attempt_status": attempt.status,
                "owner_key": order.owner_key,
                "source_attempt_id": str(attempt.id),
                "source_plan_revision": record.get("plan_revision"),
                "latest_turn": str(run.user_message_id),
                "plan_digest": plan_digest,
                "canceled": order.canceled_at is not None,
            },
            action={
                "id": str(action.id),
                "call_id": action.call_id,
                "request": action.request,
                "request_digest": action.request_digest,
            },
            receipt=receipt,
            adapted_result=adapted,
            adapted_result_digest=digest(adapted),
            observation={
                **observation,
                "fresh": True,
            },
            current={
                "owner_key": user.sub,
                "plan_revision": order.plan_revision,
                "config_sha256": config_fingerprint(get_builtin_agent_config()),
                "plan_digest": plan_digest,
                "latest_turn": str(run.user_message_id),
                "decision_unexpired": expires_at > now,
                "budgets_available": True,
            },
        )
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    decision_payload = {
        "intent": "verified_commit",
        "attempt_id": str(attempt.id),
        "action_id": str(action.id),
        "sha256": body.sha256,
        "approved": body.approved,
        "source_revision": order.plan_revision,
        "observation_digest": observation["verdict_digest"],
        "expires_at": expires_at.isoformat(),
    }
    event = await append_event(
        db, order.id, "chat.verified_commit_decided", actor=user.sub, payload=decision_payload
    )
    target = None
    if body.approved:
        _, target = await create_single_step_plan(
            db,
            order,
            kind="agent_turn",
            title="Continue after verified recipient commit",
            input_data={
                "runner": "durable_chat",
                "verified_commit_decision_id": "pending",
                "workspace_context": (step.input_ or {}).get("workspace_context", {}),
            },
            # One retry is reserved for a worker crash after the decision was
            # consumed but before the safe pending tail began execution.
            max_attempts=2,
            timeout_seconds=7200,
            actor=user.sub,
        )
        order.blocker = None
    decision = VerifiedCommitDecision(
        work_order_id=order.id,
        source_attempt_id=attempt.id,
        logical_action_id=action.id,
        owner_key=user.sub,
        request_digest=request_digest,
        approved=body.approved,
        source_checkpoint_sha256=body.sha256,
        source_plan_revision=decision_payload["source_revision"],
        observation=observation,
        restored_checkpoint=restored if body.approved else None,
        event_id=event.id,
        target_step_id=target.id if target else None,
        target_revision=order.plan_revision if target else None,
        expires_at=expires_at,
        created_at=now,
    )
    db.add(decision)
    await db.flush()
    if target is not None:
        target.input_ = {
            **target.input_,
            "verified_commit_decision_id": str(decision.id),
        }
        event.payload = {
            **decision_payload,
            "decision_id": str(decision.id),
            "target_step_id": str(target.id),
            "target_revision": order.plan_revision,
        }
    else:
        event.payload = {**decision_payload, "decision_id": str(decision.id)}
    await db.commit()
    return describe(run, order)


def describe(run, order):
    return {
        "id": run.id,
        "session_id": run.session_id,
        "work_order_id": order.id,
        "request_id": run.request_id,
        "status": order.status,
        "result_message_id": run.result_message_id,
        "blocker": order.blocker,
    }


@router.post("", status_code=202)
async def submit_chat_run(
    body: ChatRunCreate,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    if is_service_account(user):
        raise HTTPException(403, "Chat intake requires a human owner")
    try:
        result = await submit_agent_intake(
            db,
            identity=VerifiedIntakeIdentity(account_key=user.sub, channel="http"),
            request=AgentIntakeRequest(
                channel="http",
                external_message_id=str(body.request_id),
                request_id=body.request_id,
                session_id=body.session_id,
                content=body.content,
                reasoning_mode=body.reasoning_mode,
                attachments=tuple(
                    IntakeAttachment(document_id=item.document_id) for item in body.attachments
                ),
                workspace_context=body.workspace_context,
                # This is the pre-E12 HTTP idempotency representation.  It
                # intentionally retains client-supplied attachment metadata.
                input_digest=hashlib.sha256(body.model_dump_json().encode()).hexdigest(),
            ),
        )
    except AgentIntakeError as exc:
        # Preserve the HTTP transport contract while keeping policy and
        # persistence details out of the common intake service.
        detail = str(exc)
        if detail == "External message ID already used for different input":
            detail = "Request ID already used for different input"
        raise HTTPException(exc.status_code, detail) from exc
    # Beat discovers the committed ready row. Broker availability is not part
    # of intake, so a lost enqueue cannot lose the user's request.
    return describe(result.run, result.order)


async def owned_run(db, run_id, user):
    run = await db.scalar(
        select(DurableChatRun).where(
            DurableChatRun.id == run_id,
            DurableChatRun.owner_key == user.sub,
        )
    )
    if run is None:
        raise HTTPException(404, "Chat run not found")
    return run


class ActionObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    request_digest: str = Field(pattern="^[a-f0-9]{64}$")
    outcome: Literal["observed", "not_observed", "inconclusive"]
    note: str = Field(min_length=1, max_length=10000)
    evidence_reference: str = Field(min_length=1, max_length=2000)


@router.get("/{run_id}/actions")
async def get_chat_actions(
    run_id: uuid.UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    from app.domain.chat_action_journal import action_state

    run = await owned_run(db, run_id, user)
    order = await db.get(WorkOrder, run.work_order_id)
    actions = list(
        (
            await db.execute(
                select(ChatLogicalAction, ChatLogicalAction.request["name"].as_string())
                .options(defer(ChatLogicalAction.request), defer(ChatLogicalAction.result))
                .where(ChatLogicalAction.work_order_id == order.id)
                .order_by(ChatLogicalAction.created_at, ChatLogicalAction.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    items = []
    for action, tool_name in actions:
        observation = await db.scalar(
            select(WorkEvent)
            .where(
                WorkEvent.work_order_id == order.id,
                WorkEvent.event_type == "chat.action_observation",
                WorkEvent.payload["action_id"].as_string() == str(action.id),
            )
            .order_by(WorkEvent.sequence.desc())
            .limit(1)
        )
        items.append(
            {
                "id": action.id,
                "call_id": action.call_id,
                "attempt_id": action.attempt_id,
                "status": await action_state(db, order, action),
                "tool": tool_name,
                "request_digest": action.request_digest,
                "result_available": action.result_digest is not None,
                "result_digest": action.result_digest,
                "can_replay": False,
                "latest_observation": {
                    "sequence": observation.sequence,
                    "actor": observation.actor,
                    **observation.payload,
                }
                if observation
                else None,
            }
        )
    return {
        "items": items,
        "next_offset": offset + len(items),
        "coverage": "journaled_actions_only",
        "work_order_status": order.status,
    }


@router.get("/{run_id}/actions/{action_id}")
async def get_chat_action(
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    from app.domain.action_receipts import read_receipt
    from app.domain.chat_action_journal import action_state, digest

    run = await owned_run(db, run_id, user)
    action = await db.get(ChatLogicalAction, action_id)
    if action is None or action.work_order_id != run.work_order_id:
        raise HTTPException(404, "Logical action not found")
    if digest(action.request) != action.request_digest or (
        action.result_digest and digest(action.result) != action.result_digest
    ):
        raise HTTPException(409, "Logical action integrity mismatch")
    order = await db.get(WorkOrder, run.work_order_id)
    return {
        "id": action.id,
        "status": await action_state(db, order, action),
        "request": action.request,
        "request_digest": action.request_digest,
        "result": action.result,
        "result_digest": action.result_digest,
        "recipient_receipt": await read_receipt(db, action),
        "can_replay": False,
    }


@router.get("/{run_id}/actions/{action_id}/verification")
async def verify_chat_action(
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    from app.domain.artifact_verification import verify_action_artifact

    # Verification is a SELECT-only snapshot. Keep the complete owner/action/
    # receipt/task read under one no-autoflush boundary, including helper calls.
    with db.no_autoflush:
        run = await owned_run(db, run_id, user)
        action = await db.get(ChatLogicalAction, action_id)
        if action is None or action.work_order_id != run.work_order_id:
            raise HTTPException(404, "Logical action not found")
        return await verify_action_artifact(db, action=action, user=user)


@router.get("/{run_id}/actions/{action_id}/observations")
async def get_chat_action_observations(
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    cursor: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    """Return only this owner's recorded observations, without changing their action."""
    # This endpoint is deliberately a read-only view. In particular, an evidence
    # reference is returned as data, never followed or interpreted by the server.
    with db.no_autoflush:
        run = await owned_run(db, run_id, user)
        action = await db.get(ChatLogicalAction, action_id)
        if action is None or action.work_order_id != run.work_order_id:
            raise HTTPException(404, "Logical action not found")
        observations = list(
            await db.scalars(
                select(WorkEvent)
                .where(
                    WorkEvent.work_order_id == run.work_order_id,
                    WorkEvent.event_type == "chat.action_observation",
                    WorkEvent.payload["action_id"].as_string() == str(action.id),
                    WorkEvent.sequence > cursor,
                )
                .order_by(WorkEvent.sequence.asc())
                .limit(limit)
            )
        )
        # Do not expose the generic event payload: it can grow with internal
        # fields, while this view has a deliberately small evidence contract.
        items = [
            {
                "sequence": observation.sequence,
                "request_id": observation.payload["request_id"],
                "actor": observation.actor,
                "created_at": observation.created_at.isoformat(),
                "outcome": observation.payload["outcome"],
                "note": observation.payload["note"],
                "evidence_reference": observation.payload["evidence_reference"],
                "verified": False,
                "can_replay": False,
            }
            for observation in observations
        ]
    return {
        "items": items,
        "next_cursor": observations[-1].sequence if observations else cursor,
    }


@router.post("/{run_id}/actions/{action_id}/observations", status_code=201)
async def observe_chat_action(
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    body: ActionObservationRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    from app.domain.chat_action_journal import action_state, digest
    from app.domain.chat_continuation import canonical

    if is_service_account(user):
        raise HTTPException(403, "Observation requires a human owner")
    if not body.note.strip() or not body.evidence_reference.strip():
        raise HTTPException(422, "Observation requires a note and evidence reference")
    run = await owned_run(db, run_id, user)
    order = await db.get(WorkOrder, run.work_order_id, with_for_update=True)
    action = await db.get(ChatLogicalAction, action_id)
    if action is None or action.work_order_id != order.id:
        raise HTTPException(404, "Logical action not found")
    payload = {
        **body.model_dump(mode="json"),
        "action_id": str(action.id),
        "verified": False,
        "can_replay": False,
    }
    old = await db.scalar(
        select(WorkEvent)
        .where(
            WorkEvent.work_order_id == order.id,
            WorkEvent.event_type == "chat.action_observation",
            WorkEvent.payload["request_id"].as_string() == str(body.request_id),
        )
        .limit(1)
    )
    if old:
        if canonical(old.payload) != canonical(payload):
            raise HTTPException(409, "Observation request already used differently")
        return {"sequence": old.sequence, **old.payload}
    if (
        action.request_digest != body.request_digest
        or digest(action.request) != body.request_digest
    ):
        raise HTTPException(409, "Logical action request changed")
    if (
        order.status not in {"blocked", "failed", "canceled"}
        or await action_state(db, order, action) != "outcome_unknown"
    ):
        raise HTTPException(409, "Observation requires a stopped unknown outcome")
    event = await append_event(
        db, order.id, "chat.action_observation", actor=user.sub, payload=payload
    )
    await db.commit()
    # An operator's report is evidence to review, not an execution authorization.
    return {"sequence": event.sequence, **event.payload}


@router.get("")
async def latest_chat_run(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_key == user.sub,
            ChatSession.deleted_at.is_(None),
        )
    )
    if session is None:
        raise HTTPException(404, "Chat session not found")
    run = await db.scalar(
        select(DurableChatRun)
        .where(
            DurableChatRun.session_id == session_id,
            DurableChatRun.owner_key == user.sub,
        )
        .order_by(DurableChatRun.created_at.desc(), DurableChatRun.id.desc())
        .limit(1)
    )
    return {
        "run": describe(run, await db.get(WorkOrder, run.work_order_id)) if run else None,
        "legacy": run is None
        and bool(
            await db.scalar(
                select(ChatMessage.id).where(ChatMessage.session_id == session_id).limit(1)
            )
        ),
    }


@router.get("/{run_id}")
async def get_chat_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    run = await owned_run(db, run_id, user)
    return describe(run, await db.get(WorkOrder, run.work_order_id))


@router.get("/{run_id}/events")
async def get_chat_events(
    run_id: uuid.UUID,
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    run = await owned_run(db, run_id, user)
    events = list(
        await db.scalars(
            select(WorkEvent)
            .where(
                WorkEvent.work_order_id == run.work_order_id,
                WorkEvent.sequence > after,
            )
            .order_by(WorkEvent.sequence)
            .limit(limit)
        )
    )
    return {
        "items": [
            {"sequence": e.sequence, "type": e.event_type, "payload": e.payload} for e in events
        ],
        "next_cursor": events[-1].sequence if events else after,
    }


@router.get("/{run_id}/checkpoint")
async def get_chat_checkpoint(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    from app.ai.chat_checkpoint import unpack_checkpoint

    run = await owned_run(db, run_id, user)
    order = await db.get(WorkOrder, run.work_order_id)
    attempt = await db.scalar(
        select(WorkStepAttempt)
        .join(
            WorkStep,
            WorkStep.id == WorkStepAttempt.step_id,
        )
        .where(WorkStep.work_order_id == run.work_order_id)
        .order_by(
            WorkStepAttempt.started_at.desc(),
            WorkStepAttempt.attempt_no.desc(),
        )
        .limit(1)
    )
    if attempt is None or not attempt.checkpoint:
        return {"available": False, "can_resume": False}
    record = attempt.checkpoint
    step = await db.get(WorkStep, attempt.step_id)
    verified_commit_decided = await db.scalar(
        select(VerifiedCommitDecision.id).where(
            VerifiedCommitDecision.source_attempt_id == attempt.id,
            VerifiedCommitDecision.work_order_id == order.id,
        )
    )
    if (
        record.get("kind") != "durable_chat"
        or record.get("owner_key") != user.sub
        or record.get("work_order_id") != str(order.id)
        or record.get("step_id") != str(attempt.step_id)
        or record.get("attempt_id") != str(attempt.id)
        or step is None
        or record.get("plan_id") != str(step.plan_id)
        or (record.get("plan_revision") != order.plan_revision and verified_commit_decided is None)
    ):
        raise HTTPException(409, "Stale or invalid checkpoint binding")
    try:
        payload = unpack_checkpoint(record.get("snapshot") or {})
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(409, "Invalid checkpoint integrity") from exc
    from app.ai.agent_config import get_builtin_agent_config
    from app.domain.chat_continuation import (
        confirmation_state,
        validate_current_config,
        validate_wall_budget,
    )

    can_resume = False
    verified_commit = None
    if order.status == "blocked" and run.result_message_id is None:
        try:
            confirmation_state(record, order, step, attempt)
            validate_current_config(payload, get_builtin_agent_config())
            validate_wall_budget(order)
            latest = await db.scalar(
                select(DurableChatRun.id)
                .where(DurableChatRun.session_id == run.session_id)
                .order_by(DurableChatRun.created_at.desc(), DurableChatRun.id.desc())
                .limit(1)
            )
            can_resume = (
                latest == run.id
                and verified_commit_decided is None
                and await existing_decision(db, order.id, attempt.id) is None
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        if not can_resume and not is_service_account(user):
            try:
                with db.no_autoflush:
                    verified_commit = await _verified_commit_checkpoint_offer(
                        db,
                        run=run,
                        order=order,
                        step=step,
                        attempt=attempt,
                        record=record,
                        payload=payload,
                        user=user,
                    )
            except (HTTPException, ValueError, TypeError, KeyError, AttributeError):
                # Eligibility is an optional, fail-closed projection.  The
                # checkpoint endpoint must not turn an unsupported/stale
                # receipt into client authority or obscure the existing E09
                # verification verdict (whose can_resume remains false).
                verified_commit = None
    # Full model context stays private storage, not an API/debug transcript.
    summary = {
        "available": True,
        "can_resume": can_resume or verified_commit is not None,
        "confirmation": payload.get("confirmation") if can_resume else None,
        "phase": payload["phase"],
        "plan_revision": order.plan_revision,
        "attempt_id": attempt.id,
        "pending_tool_count": len(payload["pending_calls"]),
        "in_flight": bool(payload.get("in_flight_call_id")),
        "sha256": record["snapshot"]["sha256"],
    }
    if verified_commit is not None:
        summary.update(verified_commit)
    return summary


async def _verified_commit_checkpoint_offer(
    db,
    *,
    run,
    order,
    step,
    attempt,
    record,
    payload,
    user,
):
    """Read-only E11 eligibility using the authoritative receipt and artifact."""
    from app.ai.agent_config import get_builtin_agent_config
    from app.ai.tool_result import normalize_http_one_db_commit_response
    from app.domain.action_receipts import read_receipt
    from app.domain.artifact_verification import validate_artifact_verdict, verify_action_artifact
    from app.domain.chat_action_journal import digest
    from app.domain.chat_continuation import (
        config_fingerprint,
        plan_fingerprint,
        validate_shared_budgets,
        validate_wall_budget,
        verified_commit_continuation_state,
    )

    if order.canceled_at is not None:
        raise ValueError("Verified-commit source binding changed")
    latest = await db.scalar(
        select(DurableChatRun.id)
        .where(DurableChatRun.session_id == run.session_id)
        .order_by(DurableChatRun.created_at.desc(), DurableChatRun.id.desc())
        .limit(1)
    )
    if latest != run.id:
        raise ValueError("A newer conversation turn exists")
    if await db.scalar(
        select(VerifiedCommitDecision.id).where(
            VerifiedCommitDecision.source_attempt_id == attempt.id,
            VerifiedCommitDecision.work_order_id == order.id,
        )
    ):
        raise ValueError("Verified-commit checkpoint already decided")

    phase = payload.get("phase")
    completed = payload.get("completed_call") or {}
    call_id = (
        payload.get("in_flight_call_id") if phase == "tool_started" else completed.get("call_id")
    )
    action_id = (payload.get("action_ids") or {}).get(call_id)
    if phase == "tool_recorded" and completed.get("action_id") != action_id:
        raise ValueError("Logical action frontier binding changed")
    try:
        action_uuid = uuid.UUID(str(action_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Logical action frontier binding changed") from None
    action = await db.get(ChatLogicalAction, action_uuid)
    if (
        action is None
        or action.work_order_id != order.id
        or action.attempt_id != attempt.id
        or action.call_id != call_id
        or digest(action.request) != action.request_digest
    ):
        raise ValueError("Verified-commit source not found")
    if phase == "tool_started":
        valid_frontier = (
            attempt.status == "failed"
            and action.status == "started"
            and action.result is None
            and action.result_digest is None
        )
    elif phase == "tool_recorded":
        valid_frontier = (
            attempt.status in {"failed", "outcome_unknown"}
            and action.status == "outcome_unknown"
            and action.result == completed.get("result")
            and action.result_digest == digest(action.result)
        )
    else:
        valid_frontier = False
    if not valid_frontier:
        raise ValueError("Logical action frontier binding changed")

    plan = await db.get(WorkPlan, step.plan_id)
    if plan is None or plan.work_order_id != order.id or plan.revision != record["plan_revision"]:
        raise ValueError("Source plan binding changed")
    validate_wall_budget(order)
    await validate_shared_budgets(db, order)
    with db.no_autoflush:
        receipt = await read_receipt(db, action)
        if receipt is None:
            raise ValueError("Committed recipient receipt is missing")
        verifier = await _verified_commit_owner_verifier(db, order)
        observation = await verify_action_artifact(db, action=action, user=verifier)
    if not validate_artifact_verdict(observation) or observation.get("status") != "matched":
        raise ValueError("Current recipient artifact is not proved")
    adapted = normalize_http_one_db_commit_response(
        receipt["response"], operation=receipt["operation"]
    ).model_dump(mode="json")
    if digest(receipt["response"]) != receipt["response_digest"]:
        raise ValueError("Recipient response adaptation binding changed")
    plan_digest = plan_fingerprint(plan)
    verified_commit_continuation_state(
        checkpoint=record["snapshot"],
        source={
            "order_status": order.status,
            "attempt_status": attempt.status,
            "owner_key": order.owner_key,
            "source_attempt_id": str(attempt.id),
            "source_plan_revision": record["plan_revision"],
            "latest_turn": str(run.user_message_id),
            "plan_digest": plan_digest,
            "canceled": order.canceled_at is not None,
        },
        action={
            "id": str(action.id),
            "call_id": action.call_id,
            "request": action.request,
            "request_digest": action.request_digest,
        },
        receipt=receipt,
        adapted_result=adapted,
        adapted_result_digest=digest(adapted),
        observation={**observation, "fresh": True},
        current={
            "owner_key": user.sub,
            "plan_revision": order.plan_revision,
            "config_sha256": config_fingerprint(get_builtin_agent_config()),
            "plan_digest": plan_digest,
            "latest_turn": str(run.user_message_id),
            "decision_unexpired": True,
            "budgets_available": True,
        },
    )
    return {
        "intent": "verified_commit",
        "action_id": action.id,
        "attempt_id": attempt.id,
        "sha256": record["snapshot"]["sha256"],
        "can_resume": True,
    }
