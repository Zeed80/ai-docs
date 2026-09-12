"""Atomic action/checkpoint journal. Recorded output is not verified external effect."""

import hashlib
import uuid

from app.db.agent_runtime_models import ChatLogicalAction
from app.db.models import WorkStep, WorkStepAttempt
from app.domain.chat_continuation import canonical
from app.domain.work_orders import append_event, attempt_owns_lease


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


async def record_boundary(db, order, attempt, payload):
    """Caller holds the order lock and commits this with its checkpoint."""
    phase = payload["phase"]
    ids = payload.get("action_ids") or {}
    pending = payload["pending_calls"]
    if len({ids.get(call["id"]) for call in pending}) != len(pending):
        raise ValueError("Logical action IDs must be unique")
    for call in pending:
        action_id = uuid.UUID(ids[call["id"]])
        request = call.get("function") or {}
        request_digest = digest(request)
        action = await db.get(ChatLogicalAction, action_id)
        if action is None:
            if phase != "tools_planned":
                raise ValueError("Action must be journaled before dispatch")
            action = ChatLogicalAction(
                id=action_id,
                work_order_id=order.id,
                attempt_id=attempt.id,
                call_id=call["id"],
                request=request,
                request_digest=request_digest,
                status="planned",
            )
            db.add(action)
            await db.flush()
            await append_event(
                db,
                order.id,
                "chat.action_planned",
                actor="chat-worker",
                payload={
                    "action_id": str(action.id),
                    "call_id": action.call_id,
                    "request_digest": request_digest,
                },
            )
        if (
            action.work_order_id != order.id
            or action.call_id != call["id"]
            or action.request_digest != request_digest
            or digest(action.request) != request_digest
        ):
            raise ValueError("Logical action binding changed")
        if action.status in {"result_recorded", "outcome_unknown"}:
            raise ValueError("Recorded logical action cannot execute again")
        if phase == "tools_planned" and action.status == "started":
            raise ValueError("Unknown action outcome cannot be replayed")
        if call["id"] == payload.get("in_flight_call_id"):
            if phase == "tool_started":
                if action.status not in {"planned", "waiting_confirmation"}:
                    raise ValueError("Action already started")
                action.status = "started"
                action.attempt_id = attempt.id
                await append_event(
                    db,
                    order.id,
                    "chat.action_started",
                    actor="chat-worker",
                    payload={"action_id": str(action.id), "attempt_id": str(attempt.id)},
                )
            elif phase == "confirmation_required":
                if action.status != "started" or action.attempt_id != attempt.id:
                    raise ValueError("Confirmation does not match started action")
                action.status = "waiting_confirmation"
                await append_event(
                    db,
                    order.id,
                    "chat.action_waiting_confirmation",
                    actor="chat-worker",
                    payload={"action_id": str(action.id), "attempt_id": str(attempt.id)},
                )
    if phase == "tool_recorded":
        completed = payload.get("completed_call") or {}
        action = await db.get(ChatLogicalAction, uuid.UUID(completed["action_id"]))
        if (
            action is None
            or action.work_order_id != order.id
            or action.attempt_id != attempt.id
            or action.call_id != completed["call_id"]
            or action.status != "started"
        ):
            raise ValueError("Result does not match started logical action")
        action.result = completed["result"]
        action.result_digest = digest(action.result)
        action.status = (
            "outcome_unknown"
            if isinstance(action.result, dict) and action.result.get("status") == "outcome_unknown"
            else "result_recorded"
        )
        await append_event(
            db,
            order.id,
            "chat.action_result_recorded",
            actor="chat-worker",
            payload={
                "action_id": str(action.id),
                "call_id": action.call_id,
                "result_digest": action.result_digest,
            },
        )


async def action_state(db, order, action):
    if action.status != "started":
        return action.status
    attempt = await db.get(WorkStepAttempt, action.attempt_id)
    step = await db.get(WorkStep, attempt.step_id) if attempt else None
    if order.status != "running" or not step or not attempt_owns_lease(step, attempt):
        return "outcome_unknown"
    return "started"
