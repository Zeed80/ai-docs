"""Retry policy and result adapters from reviewed E03 effects."""

from typing import Any

from app.ai.tool_catalog import TOOLS, ToolDefinition, get_tool
from app.ai.tool_result import (
    ToolResult,
    normalize_http_one_db_commit_response,
    normalize_http_read_response,
)

# E05.2's independently reviewed subset of E03 ``one-db-commit`` rows from
# docs/agent-employee-delivery/tool-effect-inventory.md. Each recipient handler
# has exactly one direct db.commit and no enqueue or external dispatch. This set
# is deliberately independent from catalog ``effect=write``: that broader value
# also contains async enqueue, external dispatch and unresolved operations.
ONE_DB_COMMIT_OPERATIONS = frozenset(
    {
        "analytics.calendar_create_reminder",
        "analytics.collection_add_item",
        "analytics.collection_close",
        "analytics.collection_create",
        "analytics.compare_align",
        "analytics.compare_create",
        "analytics.table_create_view",
        "analytics.table_inline_edit",
        "warehouse.create_item",
    }
)


def resolve_catalog_operation(skill: dict, args: dict) -> ToolDefinition | None:
    """Resolve one exact operation from the HTTP route and original arguments.

    Shared capability routes are disambiguated only by their original ``action``
    argument. Direct routes must have exactly one catalog owner; aliases sharing
    a route remain legacy because the exact operation cannot be proved.
    """

    method = str(skill.get("method", "")).upper()
    path = str(skill.get("path", ""))
    prefix = "/api/agent/cap/"
    if method == "POST" and path.startswith(prefix):
        capability = path[len(prefix) :]
        if not capability or "/" in capability:
            return None
        action = args.get("action")
        if not isinstance(action, str):
            return None
        return get_tool(capability, action)

    matches = [tool for tool in TOOLS.values() if tool.method == method and tool.path == path]
    return matches[0] if len(matches) == 1 else None


def one_db_commit_operation(skill: dict, args: dict) -> ToolDefinition | None:
    """Return an exact E05.2 reviewed one-commit operation, else fail closed."""

    operation = resolve_catalog_operation(skill, args)
    if operation is None or operation.name not in ONE_DB_COMMIT_OPERATIONS:
        return None
    return operation


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


def serialize_one_db_commit_response(payload: Any, *, operation: str) -> dict[str, Any]:
    """Return the concrete v1 envelope for an E03-reviewed one-commit write."""

    return normalize_http_one_db_commit_response(payload, operation=operation).model_dump(
        mode="json"
    )


def one_db_commit_http_failure(*, operation: str, status_code: int, payload: Any) -> dict[str, Any]:
    """A 4xx rejection has no confirmed recipient effect and is a failed call."""

    return ToolResult(
        status="failed",
        data=payload,
        error_code=f"http_{status_code}",
        retryable=False,
        evidence={
            "adapter_contract": "http_one_db_commit_response_v1",
            "operation": operation,
            "http_status": status_code,
            "effect": "one_db_commit",
            "effect_confirmed": False,
        },
    ).model_dump(mode="json")


def one_db_commit_outcome_unknown(
    *,
    operation: str,
    reason: str,
    status_code: int | None = None,
    payload: Any = None,
) -> dict[str, Any]:
    """Stop after one dispatched write whose recipient outcome is ambiguous."""

    evidence: dict[str, Any] = {
        "adapter_contract": "http_one_db_commit_response_v1",
        "operation": operation,
        "effect": "one_db_commit",
        "dispatch_attempted": True,
        "recipient_outcome": "unconfirmed",
        "reason": reason,
    }
    if status_code is not None:
        evidence["http_status"] = status_code
    return ToolResult(
        status="outcome_unknown",
        data=payload,
        error_code="tool_outcome_unknown",
        retryable=False,
        evidence=evidence,
    ).model_dump(mode="json")


def one_db_commit_pre_dispatch_failure(*, operation: str, reason: str) -> dict[str, Any]:
    """Fail explicitly when the request was never dispatched to the recipient."""

    return ToolResult(
        status="failed",
        error_code="write_dispatch_failed",
        retryable=False,
        evidence={
            "adapter_contract": "http_one_db_commit_response_v1",
            "operation": operation,
            "effect": "one_db_commit",
            "dispatch_attempted": False,
            "reason": reason,
        },
    ).model_dump(mode="json")


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
