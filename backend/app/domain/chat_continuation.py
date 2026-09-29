"""Validate a human-authorized continuation, never a replay after unknown effects."""

import hashlib
import json
from datetime import UTC, datetime

from app.ai.chat_checkpoint import unpack_checkpoint


def canonical(value) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def config_fingerprint(config) -> str:
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()


def plan_fingerprint(plan) -> str:
    return hashlib.sha256(
        canonical(
            {
                "id": str(plan.id),
                "revision": plan.revision,
                "goal": plan.goal,
                "assumptions": plan.assumptions,
                "verification_plan": plan.verification_plan,
            }
        ).encode()
    ).hexdigest()


def confirmation_state(record: dict, order, step, attempt, *, source_revision=None) -> dict:
    revision = order.plan_revision if source_revision is None else source_revision
    if (
        record.get("kind") != "durable_chat"
        or record.get("owner_key") != order.owner_key
        or record.get("work_order_id") != str(order.id)
        or record.get("step_id") != str(step.id)
        or step.work_order_id != order.id
        or record.get("attempt_id") != str(attempt.id)
        or attempt.step_id != step.id
        or record.get("plan_id") != str(step.plan_id)
        or record.get("plan_revision") != revision
        or attempt.status != "failed"
    ):
        raise ValueError("Stale checkpoint or non-recoverable attempt")
    payload = unpack_checkpoint(record.get("snapshot") or {})
    pending = payload["pending_calls"]
    confirmation = payload.get("confirmation") or {}
    if (
        payload["phase"] != "confirmation_required"
        or not pending
        or payload.get("in_flight_call_id") != pending[0]["id"]
    ):
        raise ValueError("Only a stopped confirmation boundary can continue")
    function = pending[0].get("function") or {}
    args = function.get("arguments", {})
    if isinstance(args, str):
        args = json.loads(args)
    if (
        not isinstance(args, dict)
        or function.get("name", "").replace("__", ".") != confirmation.get("tool")
        or canonical(args) != canonical(confirmation.get("args"))
    ):
        raise ValueError("Confirmation does not match the pending tool")
    return payload


def validate_current_config(payload: dict, config):
    if (payload.get("runtime") or {}).get("config_sha256") != config_fingerprint(config):
        raise ValueError("Agent configuration changed; automatic continuation refused")


def decision_not_expired(decision: dict):
    expires = datetime.fromisoformat(decision["expires_at"])
    if expires <= datetime.now(UTC):
        raise ValueError("Continuation approval expired")


def validate_wall_budget(order):
    limit = (order.budgets or {}).get("max_wall_clock_seconds")
    if limit is not None and order.started_at is not None:
        if (datetime.now(UTC) - order.started_at).total_seconds() >= float(limit):
            raise ValueError("Chat wall-clock budget exhausted")


async def validate_shared_budgets(db, order):
    """Read-only counterpart of the scheduler's shared budget enforcement."""
    from sqlalchemy import func, select

    from app.db.models import WorkStep, WorkStepAttempt, WorkToolCall

    budgets = order.budgets or {}
    checks = (
        (
            "token_budget",
            int,
            select(func.coalesce(func.sum(WorkStepAttempt.tokens_used), 0))
            .join(WorkStep, WorkStep.id == WorkStepAttempt.step_id)
            .where(WorkStep.work_order_id == order.id),
        ),
        (
            "max_cost_usd",
            float,
            select(func.coalesce(func.sum(WorkStepAttempt.cost_usd), 0.0))
            .join(WorkStep, WorkStep.id == WorkStepAttempt.step_id)
            .where(WorkStep.work_order_id == order.id),
        ),
        (
            "max_tool_calls",
            int,
            select(func.count())
            .select_from(WorkToolCall)
            .where(WorkToolCall.work_order_id == order.id),
        ),
    )
    for name, cast, query in checks:
        if budgets.get(name) is not None and cast(await db.scalar(query)) >= cast(budgets[name]):
            raise ValueError(f"Chat {name} budget exhausted")


def verified_commit_continuation_state(
    *,
    checkpoint: dict,
    source: dict,
    action: dict,
    receipt: dict,
    adapted_result: dict,
    adapted_result_digest: str,
    observation: dict,
    current: dict,
) -> dict:
    """Validate and restore an E10 verified-commit frontier without replaying it.

    This pure seam intentionally does not grant worker execution.  E11.1 stores
    its output as the immutable input of a newly reserved step; E11.2 will own
    atomic consumption and executor hydration.
    """
    payload = unpack_checkpoint(checkpoint)
    runtime = payload.get("runtime") or {}
    if (
        source.get("canceled")
        or source.get("order_status") != "blocked"
        or source.get("owner_key") != current.get("owner_key")
        or source.get("latest_turn") != current.get("latest_turn")
        or source.get("source_plan_revision") != current.get("plan_revision")
        or runtime.get("config_sha256") != current.get("config_sha256")
        or source.get("plan_digest", current.get("plan_digest")) != current.get("plan_digest")
        or (
            runtime.get("plan_digest") is not None
            and runtime["plan_digest"] != current.get("plan_digest")
        )
        or (
            runtime.get("last_turn") is not None
            and runtime["last_turn"] != current.get("latest_turn")
        )
        or not current.get("decision_unexpired")
        or not current.get("budgets_available")
    ):
        raise ValueError("Verified-commit source binding changed")
    expected_attempt_status = (
        "outcome_unknown" if payload.get("phase") == "tool_recorded" else "failed"
    )
    if source.get("attempt_status") not in {expected_attempt_status, "failed"}:
        # ``failed`` remains accepted for legacy E10 snapshots.  New
        # tool_recorded v1 checkpoints are stopped as outcome_unknown.
        raise ValueError("Verified-commit attempt frontier changed")
    if receipt.get("operation") not in {
        "agent_control.task_propose",
        "warehouse.update_item",
    }:
        raise ValueError("Unsupported verified-commit recipient")
    if (
        receipt.get("receipt_version") != 1
        or receipt.get("logical_action_id") != action.get("id")
        or receipt.get("owner_key") != source.get("owner_key")
        or receipt.get("request_digest") != action.get("request_digest")
        or receipt.get("response_digest") != observation.get("artifact_hash")
        or observation.get("status") != "matched"
        or not observation.get("fresh")
        or observation.get("operation") != receipt.get("operation")
        or observation.get("artifact_id") != receipt.get("artifact_id")
        or observation.get("artifact_version") != receipt.get("artifact_revision")
    ):
        raise ValueError("Verified receipt or artifact observation changed")
    if adapted_result.get("status") != "succeeded":
        raise ValueError("Receipt did not adapt to a successful ToolResult")
    if adapted_result_digest != hashlib.sha256(canonical(adapted_result).encode()).hexdigest():
        raise ValueError("Adapted result digest changed")

    call_id = action.get("call_id")
    action_id = action.get("id")
    action_ids = payload.get("action_ids") or {}
    if action_ids.get(call_id) != action_id:
        raise ValueError("Checkpoint action binding changed")
    messages = list(payload.get("messages") or [])
    pending = list(payload.get("pending_calls") or [])
    result_message = {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(adapted_result, ensure_ascii=False),
    }
    phase = payload.get("phase")
    if phase == "tool_started":
        matching_pending = [call for call in pending if call.get("id") == call_id]
        recorded = [
            message
            for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id") == call_id
        ]
        if (
            payload.get("in_flight_call_id") != call_id
            or len(matching_pending) != 1
            or recorded
            or payload.get("completed_call") is not None
            or canonical(matching_pending[0].get("function") or {})
            != canonical(action.get("request") or {})
        ):
            raise ValueError("Invalid tool_started frontier")
        messages.append(result_message)
        pending = [call for call in pending if call.get("id") != call_id]
    elif phase == "tool_recorded":
        completed = payload.get("completed_call") or {}
        unknown = completed.get("result") or {}
        indexes = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "tool" and message.get("tool_call_id") == call_id
        ]
        if (
            payload.get("in_flight_call_id") is not None
            or any(call.get("id") == call_id for call in pending)
            or completed.get("action_id") != action_id
            or completed.get("call_id") != call_id
            or unknown.get("status") != "outcome_unknown"
            or len(indexes) != 1
        ):
            raise ValueError("Invalid tool_recorded frontier")
        try:
            history_result = json.loads(messages[indexes[0]].get("content"))
        except (TypeError, ValueError):
            raise ValueError("Invalid tool_recorded history") from None
        if history_result != unknown:
            raise ValueError("Invalid tool_recorded history")
        messages[indexes[0]] = result_message
    else:
        raise ValueError("Unsupported verified-commit frontier")
    return {
        **payload,
        "phase": "verified_commit_ready",
        "messages": messages,
        "pending_calls": pending,
        "in_flight_call_id": None,
        "completed_call": {
            "action_id": action_id,
            "call_id": call_id,
            "result": adapted_result,
            "result_digest": adapted_result_digest,
        },
        "execute_action_ids": [],
        "consumed_action_id": action_id,
        "can_resume": False,
    }


def validate_verified_commit_executor_state(payload: dict) -> None:
    """Fence a restored committed action out of the executable pending tail."""
    if payload.get("phase") != "verified_commit_ready":
        raise ValueError("Invalid verified-commit executor checkpoint")
    action_id = payload.get("consumed_action_id")
    completed = payload.get("completed_call") or {}
    call_id = completed.get("call_id")
    action_ids = payload.get("action_ids") or {}
    pending = payload.get("pending_calls") or []
    messages = payload.get("messages") or []
    matching_results = [
        message
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id
    ]
    if (
        not isinstance(action_id, str)
        or not action_id
        or not isinstance(call_id, str)
        or action_ids.get(call_id) != action_id
        or completed.get("action_id") != action_id
        or (completed.get("result") or {}).get("status") != "succeeded"
        or payload.get("in_flight_call_id") is not None
        or payload.get("execute_action_ids") != []
        or any(
            call.get("id") == call_id or action_ids.get(call.get("id")) == action_id
            for call in pending
        )
        or len(matching_results) != 1
    ):
        raise ValueError("Verified committed action remains executable")
