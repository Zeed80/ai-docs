"""Pilot durable chat transport. No task is owned by the HTTP connection."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.auth.jwt import get_current_user, is_service_account
from app.auth.models import UserInfo
from app.chat.store import (
    ChatSessionNotFoundError,
    append_chat_attachment,
    append_chat_message,
    ensure_chat_session,
)
from app.db.agent_runtime_models import ChatLogicalAction, DurableChatRun
from app.db.models import (
    ChatMessage,
    ChatSession,
    Document,
    WorkEvent,
    WorkOrder,
    WorkStep,
    WorkStepAttempt,
)
from app.db.session import get_db
from app.domain.work_orders import (
    ACTIVE_WORK_STATUSES,
    append_event,
    create_single_step_plan,
    create_work_order,
)

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


class ChatResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: uuid.UUID
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    approved: bool = Field(strict=True)


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
    if not body.content.strip():
        raise HTTPException(422, "Message must not be empty")
    if len(json.dumps(body.workspace_context).encode()) > 64000:
        raise HTTPException(422, "Workspace context is too large")
    # Serialize retries even when both requests would create a new ChatSession.
    lock = int.from_bytes(
        hashlib.sha256(f"{user.sub}:{body.request_id}".encode()).digest()[:8], "big", signed=True
    )
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    existing = await db.scalar(
        select(DurableChatRun).where(
            DurableChatRun.owner_key == user.sub,
            DurableChatRun.request_id == body.request_id,
        )
    )
    if existing:
        if existing.request_digest != digest:
            raise HTTPException(409, "Request ID already used for different input")
        return describe(existing, await db.get(WorkOrder, existing.work_order_id))
    try:
        session = await ensure_chat_session(db, user_key=user.sub, session_id=body.session_id)
    except ChatSessionNotFoundError as exc:
        raise HTTPException(404, "Chat session not found") from exc
    await db.execute(select(ChatSession.id).where(ChatSession.id == session.id).with_for_update())
    durable_session = await db.scalar(
        select(DurableChatRun.id).where(DurableChatRun.session_id == session.id).limit(1)
    )
    if not durable_session and await db.scalar(
        select(ChatMessage.id).where(ChatMessage.session_id == session.id).limit(1)
    ):
        raise HTTPException(
            409, "Legacy conversation migration is not enabled; create a new conversation"
        )
    active = await db.scalar(
        select(DurableChatRun.id)
        .join(
            WorkOrder,
            WorkOrder.id == DurableChatRun.work_order_id,
        )
        .where(DurableChatRun.session_id == session.id, WorkOrder.status.in_(ACTIVE_WORK_STATUSES))
        .limit(1)
    )
    if active:
        raise HTTPException(409, "A durable turn is already active in this conversation")
    message = await append_chat_message(
        db, session_id=session.id, role="user", content=body.content
    )
    for attachment in body.attachments:
        document = await db.scalar(
            select(Document).where(
                Document.id == attachment.document_id,
                Document.owner_sub == user.sub,
            )
        )
        if document is None:
            raise HTTPException(404, "Owned attachment not found")
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
        owner_key=user.sub,
        objective=body.content,
        source="durable_chat",
        budgets={"max_replans": 0, "max_wall_clock_seconds": 7200, "max_tool_calls": 200},
        metadata={"chat_session_id": str(session.id)},
        acceptance_criteria=[
            {
                "criterion_key": "objective_met",
                "kind": "semantic",
                "description": body.content,
                "required": True,
            }
        ],
    )
    run = DurableChatRun(
        owner_key=user.sub,
        request_id=body.request_id,
        request_digest=digest,
        work_order_id=order.id,
        session_id=session.id,
        user_message_id=message.id,
    )
    db.add(run)
    await create_single_step_plan(
        db,
        order,
        kind="agent_turn",
        title="Durable chat turn",
        input_data={
            "runner": "durable_chat",
            "reasoning_mode": body.reasoning_mode,
            "workspace_context": body.workspace_context,
        },
        max_attempts=1,
        timeout_seconds=7200,
    )
    await db.commit()
    # Beat discovers the committed ready row. Broker availability is not part
    # of intake, so a lost enqueue cannot lose the user's request.
    return describe(run, order)


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
        "can_replay": False,
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
    if (
        record.get("kind") != "durable_chat"
        or record.get("owner_key") != user.sub
        or record.get("work_order_id") != str(order.id)
        or record.get("step_id") != str(attempt.step_id)
        or record.get("attempt_id") != str(attempt.id)
        or step is None
        or record.get("plan_id") != str(step.plan_id)
        or record.get("plan_revision") != order.plan_revision
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
                latest == run.id and await existing_decision(db, order.id, attempt.id) is None
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
    # Full model context stays private storage, not an API/debug transcript.
    return {
        "available": True,
        "can_resume": can_resume,
        "confirmation": payload.get("confirmation") if can_resume else None,
        "phase": payload["phase"],
        "plan_revision": order.plan_revision,
        "attempt_id": attempt.id,
        "pending_tool_count": len(payload["pending_calls"]),
        "in_flight": bool(payload.get("in_flight_call_id")),
        "sha256": record["snapshot"]["sha256"],
    }
