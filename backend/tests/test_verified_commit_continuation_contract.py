"""E10 executable red contract; runtime implementation belongs to E11."""

import copy
import hashlib
import json

import pytest

from app.ai.chat_checkpoint import pack_checkpoint, unpack_checkpoint


def _call(call_id: str) -> dict:
    return {
        "id": call_id,
        "function": {"name": "warehouse", "arguments": {"action": "update_item", "id": call_id}},
    }


def _digest(value: dict) -> str:
    canonical = json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _case(phase: str) -> dict:
    committed, tail = _call("committed"), _call("tail")
    response = {"id": "item-1", "updated_at": "2026-09-27T10:00:00+00:00", "name": "kept"}
    adapted_result = {
        "version": 1,
        "status": "succeeded",
        "data": response,
        "error_code": None,
        "retryable": False,
        "evidence": {
            "adapter_contract": "http_one_db_commit_response_v1",
            "operation": "warehouse.update_item",
        },
        "checkpoint": None,
    }
    unknown_result = {
        "version": 1,
        "status": "outcome_unknown",
        "data": {"recipient": "unconfirmed"},
        "error_code": "tool_outcome_unknown",
        "retryable": False,
        "evidence": {"adapter_contract": "http_one_db_commit_response_v1"},
        "checkpoint": None,
    }
    messages = [
        {"role": "user", "content": "update then continue"},
        {"role": "assistant", "content": "before", "tool_calls": [committed, tail]},
    ]
    checkpoint = {
        "phase": phase,
        "messages": messages,
        "pending_calls": [committed, tail],
        "in_flight_call_id": "committed",
        "action_ids": {
            "committed": "00000000-0000-0000-0000-000000000101",
            "tail": "00000000-0000-0000-0000-000000000102",
        },
        "runtime": {"config_sha256": "config-a", "plan_digest": "plan-a", "last_turn": "turn-a"},
    }
    if phase == "tool_recorded":
        checkpoint["pending_calls"] = [tail]
        checkpoint["in_flight_call_id"] = None
        checkpoint["completed_call"] = {
            "action_id": checkpoint["action_ids"]["committed"],
            "call_id": "committed",
            "result": unknown_result,
        }
        messages.append(
            {
                "role": "tool",
                "tool_call_id": "committed",
                "content": json.dumps(unknown_result, ensure_ascii=False),
            }
        )
    return {
        "checkpoint": pack_checkpoint(checkpoint),
        "source": {
            "order_status": "blocked",
            "attempt_status": "failed" if phase == "tool_started" else "outcome_unknown",
            "owner_key": "alice",
            "source_attempt_id": "attempt-a",
            "source_plan_revision": 4,
            "latest_turn": "turn-a",
            "canceled": False,
        },
        "action": {
            "id": checkpoint["action_ids"]["committed"],
            "call_id": "committed",
            "request": committed["function"],
            "request_digest": _digest(committed["function"]),
        },
        "receipt": {
            "operation": "warehouse.update_item",
            "logical_action_id": checkpoint["action_ids"]["committed"],
            "owner_key": "alice",
            "request_digest": _digest(committed["function"]),
            "response": response,
            "response_digest": _digest(response),
            "artifact_id": "item-1",
            "artifact_revision": response["updated_at"],
            "receipt_version": 1,
            "provenance": {"source": "recipient"},
        },
        "adapted_result": adapted_result,
        "adapted_result_digest": _digest(adapted_result),
        "observation": {
            "status": "matched",
            "operation": "warehouse.update_item",
            "artifact_id": "item-1",
            "artifact_version": response["updated_at"],
            "artifact_hash": _digest(response),
            "fresh": True,
            "can_resume": False,
        },
        "current": {
            "owner_key": "alice",
            "plan_revision": 4,
            "config_sha256": "config-a",
            "plan_digest": "plan-a",
            "latest_turn": "turn-a",
            "decision_unexpired": True,
            "budgets_available": True,
        },
    }


def _validate_fixture_case(case: dict) -> None:
    """Execute the E10 denial matrix without implementing its transition."""
    source, current = case["source"], case["current"]
    receipt, observation, action = case["receipt"], case["observation"], case["action"]
    checkpoint = unpack_checkpoint(case["checkpoint"])
    if source["canceled"] or source["order_status"] != "blocked":
        raise ValueError("source is not continuable")
    if (
        source["attempt_status"] not in {"failed", "outcome_unknown"}
        or source["latest_turn"] != current["latest_turn"]
    ):
        raise ValueError("source frontier changed")
    if current["plan_digest"] != "plan-a" or current["config_sha256"] != "config-a":
        raise ValueError("runtime binding changed")
    if not current["decision_unexpired"] or not current["budgets_available"]:
        raise ValueError("decision or budget invalid")
    if receipt["operation"] not in {"agent_control.task_propose", "warehouse.update_item"}:
        raise ValueError("unsupported receipt")
    if checkpoint["phase"] == "tool_recorded":
        completed = checkpoint.get("completed_call") or {}
        if (
            completed.get("action_id") != action["id"]
            or completed.get("call_id") != action["call_id"]
            or (completed.get("result") or {}).get("status") != "outcome_unknown"
        ):
            raise ValueError("recorded unknown result binding changed")
        recorded = [
            message
            for message in checkpoint["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == action["call_id"]
        ]
        try:
            history_result = json.loads(recorded[0]["content"]) if len(recorded) == 1 else None
        except (TypeError, ValueError, KeyError):
            history_result = None
        if history_result != completed["result"]:
            raise ValueError("recorded unknown history changed")
    if (
        receipt["logical_action_id"] != action["id"]
        or receipt["owner_key"] != source["owner_key"]
        or receipt["request_digest"] != action["request_digest"]
        or receipt["response_digest"] != _digest(receipt["response"])
    ):
        raise ValueError("receipt binding changed")
    if (
        observation["status"] != "matched"
        or not observation["fresh"]
        or observation["operation"] != receipt["operation"]
        or observation["artifact_id"] != receipt["artifact_id"]
        or observation["artifact_version"] != receipt["artifact_revision"]
        or observation["artifact_hash"] != receipt["response_digest"]
    ):
        raise ValueError("current artifact is not proved")


def _evaluate(case: dict) -> dict:
    # E11 must provide this pure validation/restoration seam before the route or
    # worker exposes the transition. Keeping the import here makes E10 a red
    # contract without breaking test collection.
    _validate_fixture_case(case)
    from app.domain.chat_continuation import verified_commit_continuation_state

    return verified_commit_continuation_state(**case)


@pytest.mark.parametrize("phase", ["tool_started", "tool_recorded"])
def test_verified_commit_substitutes_exact_result_and_preserves_tail(phase):
    case = _case(phase)
    original = copy.deepcopy(unpack_checkpoint(case["checkpoint"]))

    restored = _evaluate(case)

    assert restored["execute_action_ids"] == []
    assert restored["consumed_action_id"] == case["action"]["id"]
    assert [call["id"] for call in restored["pending_calls"]] == ["tail"]
    assert restored["messages"][:2] == original["messages"][:2]
    committed_results = [
        message
        for message in restored["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") == "committed"
    ]
    assert len(committed_results) == 1
    assert committed_results[0]["content"] == json.dumps(case["adapted_result"], ensure_ascii=False)


DENIALS = {
    "canceled": lambda case: case["source"].update(canceled=True),
    "new_turn": lambda case: case["current"].update(latest_turn="turn-b"),
    "changed_plan": lambda case: case["current"].update(plan_digest="plan-b"),
    "changed_config": lambda case: case["current"].update(config_sha256="config-b"),
    "expired": lambda case: case["current"].update(decision_unexpired=False),
    "budget": lambda case: case["current"].update(budgets_available=False),
    "receipt_request": lambda case: case["receipt"].update(request_digest="forged"),
    "receipt_response": lambda case: case["receipt"].update(response_digest="forged"),
    "changed_artifact": lambda case: case["observation"].update(status="changed"),
    "stale_observation": lambda case: case["observation"].update(fresh=False),
    "unsupported": lambda case: case["receipt"].update(operation="browser.click"),
}


@pytest.mark.parametrize("violation", sorted(DENIALS))
def test_verified_commit_denies_unbound_or_stale_state(violation):
    case = _case("tool_started")
    DENIALS[violation](case)

    with pytest.raises(ValueError):
        _evaluate(case)


def test_e10_does_not_enable_checkpoint_resume():
    payload = unpack_checkpoint(_case("tool_started")["checkpoint"])
    assert payload["can_resume"] is False
    assert _case("tool_started")["observation"]["can_resume"] is False
