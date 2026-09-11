"""Worker-owned chat execution with durable output and fail-closed lease checks."""

import asyncio
import json

from sqlalchemy import func, select

from app.chat.store import append_chat_message
from app.db.agent_runtime_models import DurableChatRun
from app.db.models import ChatMessage, WorkEvent, WorkOrder, WorkStep, WorkStepAttempt
from app.domain.work_orders import append_event, attempt_owns_lease


class ChatRunStopped(BaseException):
    """Must cross model/tool recovery handlers without being converted into prose."""


async def run_durable_chat(
    work_order_id, step_id, attempt_id, *, session_factory, agent_factory=None
):
    try:
        return await _run_durable_chat(
            work_order_id,
            step_id,
            attempt_id,
            session_factory=session_factory,
            agent_factory=agent_factory,
        )
    except ChatRunStopped as exc:
        raise RuntimeError(str(exc)) from exc


async def _run_durable_chat(
    work_order_id, step_id, attempt_id, *, session_factory, agent_factory=None
):
    from app.ai.orchestrator import AgentOrchestrator

    factory = session_factory

    async def active(db):
        order = await db.get(WorkOrder, work_order_id, with_for_update=True)
        step = await db.get(WorkStep, step_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        if (
            order is None
            or order.status != "running"
            or step is None
            or attempt is None
            or not attempt_owns_lease(step, attempt)
        ):
            raise ChatRunStopped("Execution stopped or lease expired; no automatic replay")
        return order

    async with factory() as db:
        order = await active(db)
        run = await db.scalar(
            select(DurableChatRun).where(DurableChatRun.work_order_id == order.id)
        )
        if run is None or run.owner_key != order.owner_key:
            raise RuntimeError("Durable chat binding missing")
        session_id = run.session_id
        message = await db.get(ChatMessage, run.user_message_id)
        if message is None or message.session_id != session_id:
            raise RuntimeError("Durable chat input missing")
        prompt = message.content
        history = list(
            await db.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == session_id,
                    ChatMessage.id != message.id,
                    ChatMessage.role.in_(["user", "assistant"]),
                    ChatMessage.created_at <= message.created_at,
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(100)
            )
        )
        restored = [{"role": m.role, "content": m.content or ""} for m in reversed(history)]
        step = await db.get(WorkStep, step_id)
        reasoning_mode = (step.input_ or {}).get("reasoning_mode", "normal")
        workspace_context = (step.input_ or {}).get("workspace_context", {})
    chunks, errors = [], []

    async def collect(event):
        try:
            await persist_event(event)
        except Exception as exc:
            raise ChatRunStopped("Durable event persistence failed; execution stopped") from exc

    async def persist_event(event):
        kind = str(event.get("type") or "unknown")
        if kind == "token":
            return  # Persist complete text frames, not one transaction per token.
        serialized = json.dumps(event, ensure_ascii=False, default=str)
        if len(serialized.encode()) > 1_000_000:
            raise ChatRunStopped("Event exceeds durable storage limit")
        async with factory() as db:
            order = await active(db)
            if kind == "tool_call":
                calls = await db.scalar(
                    select(func.count())
                    .select_from(WorkEvent)
                    .where(
                        WorkEvent.work_order_id == order.id,
                        WorkEvent.event_type == "chat.tool_call",
                    )
                )
                if calls >= min(int((order.budgets or {}).get("max_tool_calls", 200)), 200):
                    raise ChatRunStopped("Tool-call budget exhausted")
            await append_event(
                db,
                order.id,
                f"chat.{kind}"[:100],
                actor="chat-worker",
                payload={"session_id": str(session_id), "event": json.loads(serialized)},
            )
            await db.commit()
        if kind == "text":
            chunks.append(str(event.get("content") or ""))
        if kind == "error":
            errors.append(str(event.get("error_code") or "agent_error"))

    agent = (agent_factory or AgentOrchestrator)(collect)
    agent._executor._session_id = str(session_id)
    agent.hydrate_history(restored)

    async def require_confirmation(skill_name, args):
        await collect({"type": "confirmation_required", "tool": skill_name, "args": args})
        raise ChatRunStopped(
            "Durable approval continuation is not yet enabled; action not executed"
        )

    agent._executor._request_approval = require_confirmation

    async def watch():
        while True:
            await asyncio.sleep(1)
            async with factory() as db:
                await active(db)

    execution = asyncio.create_task(
        agent.on_user_message(
            prompt, reasoning_mode=reasoning_mode, workspace_context=workspace_context
        )
    )
    watcher = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait(
            {execution, watcher}, timeout=7200, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise ChatRunStopped("Active time budget exhausted")
        for task in done:
            await task
    except ChatRunStopped as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        for task in (execution, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(execution, watcher, return_exceptions=True)
    if errors:
        raise RuntimeError("Agent returned errors: " + ", ".join(errors))
    result = "".join(chunks).strip()
    if not result:
        raise RuntimeError("Agent did not produce a final response")
    # Persist the response once; semantic verification remains a separate step.
    async with factory() as db:
        await active(db)
        run = await db.scalar(
            select(DurableChatRun).where(DurableChatRun.work_order_id == work_order_id)
        )
        if run.result_message_id is None:
            message = await append_chat_message(
                db,
                session_id=session_id,
                role="assistant",
                content=result,
                metadata={"work_order_id": str(work_order_id), "verified": False},
            )
            run.result_message_id = message.id
            await append_event(
                db,
                work_order_id,
                "chat.response_saved",
                actor="chat-worker",
                payload={"message_id": str(message.id), "session_id": str(session_id)},
            )
            await db.commit()
    return {"text": result, "executor": "durable_chat", "tokens_used": agent._executor.total_tokens}
