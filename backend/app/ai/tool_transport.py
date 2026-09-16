"""Retry policy and read-result adapters from reviewed effects."""

from typing import Any

from app.ai.tool_catalog import TOOLS, get_tool
from app.ai.tool_result import ToolResult, normalize_http_read_response


def retry_safe(skill: dict, args: dict) -> bool:
    method = str(skill.get("method", "")).upper()
    path = str(skill.get("path", ""))
    prefix = "/api/agent/cap/"
    if method == "POST" and path.startswith(prefix):
        capability = path[len(prefix) :]
        action = args.get("action")
        if not isinstance(action, str):
            return False
        tool = get_tool(capability, action)
        return tool is not None and tool.effect == "read"
    # Shared POST endpoints can dispatch a different operation from body fields.
    # Only exact reviewed GET templates are safe outside the capability router.
    matches = [tool for tool in TOOLS.values() if tool.method == method and tool.path == path]
    return method == "GET" and bool(matches) and all(tool.effect == "read" for tool in matches)


def unknown_outcome(reason: str) -> dict:
    return {
        "status": "outcome_unknown",
        "error_code": "tool_outcome_unknown",
        "error": reason,
        "retryable": False,
        "hint": "Do not repeat this action; verify the result with the recipient.",
    }


def serialize_http_read_response(payload: Any) -> dict[str, Any]:
    """Return the concrete v1 envelope for a catalog-proven HTTP read."""

    return normalize_http_read_response(payload).model_dump(mode="json")


def read_transport_failure(reason: str) -> dict[str, Any]:
    """A bounded read failure is explicit and cannot trigger an outer replay."""

    return ToolResult(
        status="failed",
        data={"reason": reason},
        error_code="read_transport_failed",
        retryable=False,
        evidence={
            "adapter_contract": "http_read_response_v1",
            "effect": "read",
            "effect_ambiguity": "no_side_effect_expected",
            "retry_policy": "internal_retry_budget_exhausted",
        },
    ).model_dump(mode="json")


def read_http_failure(status_code: int, payload: Any) -> dict[str, Any]:
    """Serialize an HTTP failure for a catalog-proven read without guessing success."""

    return ToolResult(
        status="failed",
        data=payload,
        error_code=f"http_{status_code}",
        evidence={"adapter_contract": "http_read_response_v1", "effect": "read"},
    ).model_dump(mode="json")
