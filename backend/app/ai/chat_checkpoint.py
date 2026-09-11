"""Versioned execution snapshots; persistence is not permission to replay tools."""

import hashlib
import json

MAX_CHECKPOINT_BYTES = 4_000_000
PHASES = {
    "tools_planned",
    "tool_started",
    "tool_recorded",
    "confirmation_required",
    "turn_finished",
}


class ChatCheckpointError(BaseException):
    """Cross model recovery handlers when durable state cannot be recorded."""


def pack_checkpoint(state: dict) -> dict:
    if state.get("phase") not in PHASES:
        raise ValueError("Unknown checkpoint phase")
    if not isinstance(state.get("messages"), list) or not isinstance(
        state.get("pending_calls"), list
    ):
        raise ValueError("Invalid checkpoint conversation")
    pending = state["pending_calls"]
    if any(not isinstance(call, dict) for call in pending):
        raise ValueError("Invalid pending tool call")
    ids = [call.get("id") for call in pending]
    if any(not isinstance(call_id, str) or not call_id for call_id in ids) or len(set(ids)) != len(
        ids
    ):
        raise ValueError("Pending tool calls require unique stable IDs")
    if state.get("in_flight_call_id") and state["in_flight_call_id"] not in ids:
        raise ValueError("In-flight tool is not pending")
    payload = {**state, "version": 1, "can_resume": False}
    raw = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    if len(raw.encode()) > MAX_CHECKPOINT_BYTES:
        raise ValueError("Checkpoint exceeds storage limit")
    # JSON round-trip prevents subsequent message mutations changing the snapshot.
    return {"payload": json.loads(raw), "sha256": hashlib.sha256(raw.encode()).hexdigest()}


def unpack_checkpoint(envelope: dict) -> dict:
    payload = envelope.get("payload") or {}
    if payload.get("version") != 1:
        raise ValueError("Unsupported checkpoint version")
    packed = pack_checkpoint(payload)
    if packed["sha256"] != envelope.get("sha256"):
        raise ValueError("Checkpoint integrity mismatch")
    return packed["payload"]
