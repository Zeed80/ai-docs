"""Recipient receipts for explicitly integrated transactional database effects.

The caller keeps its normal authorization and commits mutation plus receipt
together. The order lock serializes duplicate delivery and cancellation.
These receipts prove a past commit, not external delivery or current state.
"""

import json
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select

from app.auth.models import UserRole
from app.db.agent_runtime_models import ChatLogicalAction
from app.db.models import AgentTask, WorkEvent, WorkOrder, WorkPlan, WorkStep, WorkStepAttempt
from app.domain.chat_action_journal import digest
from app.domain.chat_continuation import validate_wall_budget
from app.domain.work_orders import append_event, attempt_owns_lease


async def read_receipt(db, action):
    rows = (
        await db.scalars(
            select(WorkEvent).where(
                WorkEvent.work_order_id == action.work_order_id,
                WorkEvent.event_type == "chat.recipient_committed",
                WorkEvent.payload["action_id"].as_string() == str(action.id),
            )
        )
    ).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise HTTPException(409, "Duplicate recipient receipt")
    receipt = rows[0].payload
    if (
        not isinstance(receipt, dict)
        or receipt.get("request_digest") != action.request_digest
        or digest(action.request) != action.request_digest
        or digest(receipt.get("response")) != receipt.get("response_digest")
    ):
        raise HTTPException(409, "Recipient receipt integrity mismatch")
    return receipt


async def prepare_proposal_receipt(db, key, user, payload, schema):
    """Validate a journaled proposal, lock its order, and return any prior receipt."""
    try:
        action_key, attempt_key = key.split(":")
        action_id, attempt_id = uuid.UUID(action_key), uuid.UUID(attempt_key)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Invalid logical action key") from None
    action = await db.get(ChatLogicalAction, action_id)
    if action is None:
        raise HTTPException(404, "Logical action not found")
    order = await db.get(WorkOrder, action.work_order_id, with_for_update=True)
    if order is None or order.owner_key != user.sub:
        raise HTTPException(404, "Logical action not found")
    # Refresh after acquiring the common fence: a worker may have changed state
    # while this request waited for a concurrent transaction.
    await db.refresh(action)
    if action.attempt_id != attempt_id:
        raise HTTPException(409, "Recipient attempt mismatch")
    try:
        request = action.request
        if digest(request) != action.request_digest or request.get("name") != "agent_control":
            raise ValueError()
        raw_args = request["arguments"]
        args = dict(raw_args) if isinstance(raw_args, dict) else json.loads(raw_args)
        if args.pop("action") != "task_propose":
            raise ValueError()
        args.pop("reason", None)
        for nested in ("filters", "body"):
            if isinstance(args.get(nested), dict):
                args.update(args.pop(nested))
        if schema.model_validate(args).model_dump(mode="json") != payload.model_dump(mode="json"):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(409, "Recipient request does not match logical action") from None
    # Keep the verifier read-only even if a caller has unrelated pending ORM
    # changes in the session: a SELECT must not trigger an autoflush.
    with db.no_autoflush:
        receipt = await read_receipt(db, action)
    if receipt is not None:
        if receipt.get("operation") != "agent_control.task_propose":
            raise HTTPException(409, "Recipient operation mismatch")
        return action, receipt
    attempt = await db.get(WorkStepAttempt, action.attempt_id)
    step = await db.get(WorkStep, attempt.step_id) if attempt else None
    plan = await db.get(WorkPlan, step.plan_id) if step else None
    if (
        order.source != "durable_chat"
        or order.status != "running"
        or action.status != "started"
        or not step
        or not plan
        or step.work_order_id != order.id
        or plan.work_order_id != order.id
        or plan.revision != order.plan_revision
        or plan.status != "active"
        or not attempt_owns_lease(step, attempt)
    ):
        raise HTTPException(409, "Recipient execution fence is no longer valid")
    try:
        validate_wall_budget(order)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return action, None


async def record_proposal_receipt(db, action, response):
    """No commit here: the recipient owns the transaction containing the effect."""
    await append_event(
        db,
        action.work_order_id,
        "chat.recipient_committed",
        actor="recipient:agent_control.task_propose",
        payload={
            "action_id": str(action.id),
            "attempt_id": str(action.attempt_id),
            "operation": "agent_control.task_propose",
            "request_digest": action.request_digest,
            "response": response,
            "response_digest": digest(response),
            "evidence_scope": "database_commit",
            "can_replay": False,
        },
    )


async def verify_proposal_receipt(db, action, user):
    """Read current task content; never mutate, fetch a URL, or authorize replay.

    Ownership of the action must be checked by the caller. Current access to
    AgentTask is admin-only, just like its original control-plane API.
    """
    if UserRole.admin not in user.roles:
        raise HTTPException(403, "Current task verification requires admin access")
    receipt = await read_receipt(db, action)
    result = {
        "action_id": str(action.id),
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "agent_task_content_snapshot",
        "can_replay": False,
        "can_resume": False,
    }
    if receipt is None:
        return {**result, "status": "inconclusive", "reason": "recipient_receipt_missing"}
    if receipt.get("operation") != "agent_control.task_propose":
        return {**result, "status": "inconclusive", "reason": "unsupported_recipient"}
    response = receipt["response"]
    fields = ("id", "objective", "description", "role", "status", "team_id", "output", "metadata")
    try:
        expected = {name: response[name] for name in fields}
        artifact_id = uuid.UUID(expected["id"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(409, "Recipient artifact binding is invalid") from None
    result.update(
        {
            "artifact_id": str(artifact_id),
            "receipt_response_digest": receipt["response_digest"],
            "expected_content_digest": digest(expected),
            "checked_fields": list(fields),
        }
    )
    task = await db.get(AgentTask, artifact_id, populate_existing=True)
    if task is None:
        return {**result, "status": "missing", "current_content_digest": None}
    current = {
        "id": str(task.id),
        "objective": task.objective,
        "description": task.description,
        "role": task.role,
        "status": task.status,
        "team_id": str(task.team_id) if task.team_id else None,
        "output": task.output,
        "metadata": task.metadata_,
    }
    current_digest = digest(current)
    return {
        **result,
        "status": "matched" if current_digest == result["expected_content_digest"] else "changed",
        "current_content_digest": current_digest,
    }
