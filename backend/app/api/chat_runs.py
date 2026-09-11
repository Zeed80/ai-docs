"""Pilot durable chat transport. No task is owned by the HTTP connection."""

import hashlib
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user, is_service_account
from app.auth.models import UserInfo
from app.chat.store import ChatSessionNotFoundError, append_chat_message, ensure_chat_session
from app.db.agent_runtime_models import DurableChatRun
from app.db.models import ChatMessage, ChatSession, WorkEvent, WorkOrder
from app.db.session import get_db
from app.domain.work_orders import ACTIVE_WORK_STATUSES, create_single_step_plan, create_work_order

router = APIRouter(prefix="/api/agent/chat-runs", tags=["durable-chat"])


class ChatRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    session_id: uuid.UUID | None = None
    content: str = Field(min_length=1, max_length=12000)
    reasoning_mode: Literal["normal", "strict"] = "normal"


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
        input_data={"runner": "durable_chat", "reasoning_mode": body.reasoning_mode},
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
