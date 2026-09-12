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
