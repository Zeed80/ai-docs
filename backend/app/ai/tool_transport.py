"""Retry policy and result adapters from reviewed E03 effects."""

from typing import Any

from app.ai.tool_catalog import TOOLS, ToolDefinition, get_tool
from app.ai.tool_result import (
    ASYNC_JOB_IDENTITY_FIELDS,
    ToolResult,
    normalize_http_async_job_response,
    normalize_http_email_send_queue_response,
    normalize_http_one_db_commit_response,
    normalize_http_read_response,
    normalize_mcp_builtin_drawing_analysis_response,
    normalize_mcp_builtin_tool_search_response,
)

_MCP_GATEWAY_PATH = "/api/agent/cap/mcp"
_MCP_BUILTIN_TOOL_SEARCH_ACTION = "tool_search_mcp"
_MCP_BUILTIN_DRAWING_ANALYSIS_ACTION = "drawing_analysis_mcp"
_EMAIL_SEND_CAPABILITY_PATH = "/api/agent/cap/email"
_EMAIL_SEND_ACTION = "send"
_COMPUTER_USE_CAPABILITY_PATH = "/api/agent/cap/computer_use"

# Browser/script calls may persist browser evidence or spend a short-lived
# grant even when their catalog effect appears to be ``read``. They therefore
# never inherit generic read retries; the set mirrors the reviewed inventory.
BROWSER_SCRIPT_MCP_OPERATIONS = frozenset(
    {
        "computer_use.browser_fetch",
        "computer_use.web_discover",
        "computer_use.desktop_snapshot",
        "computer_use.desktop_start",
        "computer_use.desktop_click",
        "computer_use.desktop_type",
        "computer_use.desktop_read",
        "computer_use.desktop_close",
        "computer_use.file_read",
        "computer_use.file_write",
        "computer_use.shell",
    }
)

# Reviewed E05.3 queue-acceptance operations. Direct routes are intentionally
# excluded: only the exact capability endpoint plus the original action proves
# which response identity contract applies.
ASYNC_JOB_OPERATIONS = frozenset(ASYNC_JOB_IDENTITY_FIELDS)

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
        "documents.link",
        "email.draft",
        "email.templates.create",
        "email.templates.update",
        "invoices.update",
        "invoices.validate",
        "memory.source_propose",
        "normalization.create_norm_card",
        "normalization.update_canonical_item",
        "normalization.update_norm_card",
        "payments.create_schedule",
        "procurement.create_request",
        "suppliers.update",
        "tech.correction_record",
        "tech.operation_template_create",
        "tool_catalog.create_supplier",
        "warehouse.adjust_stock",
        "warehouse.create_item",
        "warehouse.create_receipt",
        "warehouse.update_item",
    }
)

# E03 documents these catalog reads as persistent writes: trust_score refreshes
# a calculated score, while template rendering records render-time state. They
# are deliberately outside E05.2's reviewed ToolResult subset, but must still
# never receive the read retry policy merely because the catalog effect says
# ``read``.
READ_CATALOG_OPERATIONS_WITH_PERSISTENT_EFFECTS = frozenset(
    {
        "email.render_template",
        "email.templates.render",
        "suppliers.trust_score",
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


def async_job_operation(skill: dict, args: dict) -> ToolDefinition | None:
    """Return an exact E05.3 async operation only at its reviewed gateway."""

    operation = resolve_catalog_operation(skill, args)
    if operation is None or operation.name not in ASYNC_JOB_OPERATIONS:
        return None
    capability = operation.name.split(".", 1)[0]
    if (
        str(skill.get("method", "")).upper() != "POST"
        or str(skill.get("path", "")) != f"/api/agent/cap/{capability}"
    ):
        return None
    return operation


def email_send_queue_operation(skill: dict, args: dict) -> ToolDefinition | None:
    """Return only the reviewed email-send capability gateway operation."""

    operation = resolve_catalog_operation(skill, args)
    if operation is None or operation.name != "email.send":
        return None
    if (
        str(skill.get("method", "")).upper() != "POST"
        or str(skill.get("path", "")) != _EMAIL_SEND_CAPABILITY_PATH
        or args.get("action") != _EMAIL_SEND_ACTION
    ):
        return None
    return operation


def browser_script_mcp_operation(skill: dict, args: dict) -> ToolDefinition | None:
    """Resolve an exact reviewed computer-use call that must never retry."""

    operation = resolve_catalog_operation(skill, args)
    if operation is None or operation.name not in BROWSER_SCRIPT_MCP_OPERATIONS:
        return None
    if (
        str(skill.get("method", "")).upper() != "POST"
        or str(skill.get("path", "")) != _COMPUTER_USE_CAPABILITY_PATH
    ):
        return None
    return operation


def mcp_builtin_tool_search_operation(skill: dict, args: dict) -> bool:
    """Return true only for the reviewed built-in MCP gateway call.

    Dynamic external MCP names, look-alike routes and other built-ins must
    retain their legacy semantics until each gets an explicit contract review.
    """

    return (
        str(skill.get("method", "")).upper() == "POST"
        and str(skill.get("path", "")) == _MCP_GATEWAY_PATH
        and args.get("action") == _MCP_BUILTIN_TOOL_SEARCH_ACTION
    )


def mcp_builtin_drawing_analysis_operation(skill: dict, args: dict) -> bool:
    """Return true only for the reviewed read-only drawing MCP call."""

    arguments = args.get("arguments")
    drawing_id = arguments.get("drawing_id") if isinstance(arguments, dict) else None
    return (
        str(skill.get("method", "")).upper() == "POST"
        and str(skill.get("path", "")) == _MCP_GATEWAY_PATH
        and args.get("action") == _MCP_BUILTIN_DRAWING_ANALYSIS_ACTION
        and isinstance(arguments, dict)
        and isinstance(drawing_id, str)
        and bool(drawing_id.strip())
        and ("reanalyze" not in arguments or arguments["reanalyze"] is False)
    )


def retry_safe(skill: dict, args: dict) -> bool:
    method = str(skill.get("method", "")).upper()
    path = str(skill.get("path", ""))
    prefix = "/api/agent/cap/"
    if method == "POST" and path.startswith(prefix):
        if browser_script_mcp_operation(skill, args) is not None:
            return False
        capability = path[len(prefix) :]
        action = args.get("action")
        if not isinstance(action, str):
            return False
        tool = get_tool(capability, action)
        return (
            tool is not None
            and tool.effect == "read"
            and tool.name not in READ_CATALOG_OPERATIONS_WITH_PERSISTENT_EFFECTS
        )
    # Shared POST endpoints can dispatch a different operation from body fields.
    # Only exact reviewed GET templates are safe outside the capability router.
    matches = [tool for tool in TOOLS.values() if tool.method == method and tool.path == path]
    return (
        method == "GET"
        and bool(matches)
        and all(tool.effect == "read" for tool in matches)
        and not any(
            tool.name in READ_CATALOG_OPERATIONS_WITH_PERSISTENT_EFFECTS for tool in matches
        )
    )


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


def serialize_async_job_response(payload: Any, *, operation: str) -> dict[str, Any]:
    """Return queue acceptance as a non-terminal ToolResult v1 envelope."""

    return normalize_http_async_job_response(payload, operation=operation).model_dump(mode="json")


def serialize_mcp_builtin_tool_search_response(payload: Any) -> dict[str, Any]:
    """Return the v1 result only for the reviewed built-in MCP search tool."""

    return normalize_mcp_builtin_tool_search_response(payload).model_dump(mode="json")


def serialize_mcp_builtin_drawing_analysis_response(
    payload: Any,
    *,
    arguments: dict,
) -> dict[str, Any]:
    """Return v1 only for the reviewed read-only drawing MCP response."""

    return normalize_mcp_builtin_drawing_analysis_response(
        payload,
        requested_drawing_id=arguments["drawing_id"],
        include_dimensions=arguments.get("include_dimensions", True),
        include_surfaces=arguments.get("include_surfaces", True),
        include_gdt=arguments.get("include_gdt", True),
    ).model_dump(mode="json")


def serialize_email_send_queue_response(payload: Any, *, requested_draft_id: Any) -> dict[str, Any]:
    """Return external email queue acceptance without claiming SMTP delivery."""

    return normalize_http_email_send_queue_response(
        payload, requested_draft_id=requested_draft_id
    ).model_dump(mode="json")


def email_send_http_failure(*, status_code: int, payload: Any) -> dict[str, Any]:
    """A 4xx email gateway response is a confirmed rejection, never a retry."""

    return ToolResult(
        status="failed",
        data=payload,
        error_code=f"http_{status_code}",
        retryable=False,
        evidence={
            "adapter_contract": "http_email_send_queue_response_v1",
            "operation": "email.send",
            "effect": "external_dispatch",
            "smtp_delivery": "not_confirmed",
            "recipient_outcome": "rejected",
            "dispatch_attempted": True,
            "http_status": status_code,
        },
    ).model_dump(mode="json")


def email_send_outcome_unknown(
    *, reason: str, status_code: int | None = None, payload: Any = None
) -> dict[str, Any]:
    """Stop after one ambiguous external email dispatch attempt."""

    evidence: dict[str, Any] = {
        "adapter_contract": "http_email_send_queue_response_v1",
        "operation": "email.send",
        "effect": "external_dispatch",
        "smtp_delivery": "not_confirmed",
        "recipient_outcome": "unconfirmed",
        "dispatch_attempted": True,
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


def email_send_pre_dispatch_failure(*, reason: str) -> dict[str, Any]:
    """A local failure before email dispatch is confirmed not to have queued SMTP."""

    return ToolResult(
        status="failed",
        error_code="email_send_dispatch_failed",
        retryable=False,
        evidence={
            "adapter_contract": "http_email_send_queue_response_v1",
            "operation": "email.send",
            "effect": "external_dispatch",
            "smtp_delivery": "not_confirmed",
            "recipient_outcome": "not_dispatched",
            "dispatch_attempted": False,
            "reason": reason,
        },
    ).model_dump(mode="json")


def mcp_builtin_tool_search_http_failure(*, status_code: int, payload: Any) -> dict[str, Any]:
    """A gateway 4xx is a confirmed pre-handler rejection, not a retry."""

    return ToolResult(
        status="failed",
        data=payload,
        error_code=f"http_{status_code}",
        retryable=False,
        evidence={
            "adapter_contract": "mcp_builtin_tool_search_v1",
            "action": _MCP_BUILTIN_TOOL_SEARCH_ACTION,
            "gateway": _MCP_GATEWAY_PATH,
            "recipient_outcome": "rejected",
            "handler_dispatched": False,
            "http_status": status_code,
        },
    ).model_dump(mode="json")


def mcp_builtin_tool_search_outcome_unknown(
    *,
    reason: str,
    status_code: int | None = None,
    payload: Any = None,
) -> dict[str, Any]:
    """A dispatched MCP call failed ambiguously and must not be replayed."""

    evidence: dict[str, Any] = {
        "adapter_contract": "mcp_builtin_tool_search_v1",
        "action": _MCP_BUILTIN_TOOL_SEARCH_ACTION,
        "gateway": _MCP_GATEWAY_PATH,
        "recipient_outcome": "unconfirmed",
        "dispatch_attempted": True,
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


def mcp_builtin_tool_search_pre_dispatch_failure(*, reason: str) -> dict[str, Any]:
    """A local failure before the gateway call is confirmed not dispatched."""

    return ToolResult(
        status="failed",
        error_code="mcp_dispatch_failed",
        retryable=False,
        evidence={
            "adapter_contract": "mcp_builtin_tool_search_v1",
            "action": _MCP_BUILTIN_TOOL_SEARCH_ACTION,
            "gateway": _MCP_GATEWAY_PATH,
            "recipient_outcome": "not_dispatched",
            "dispatch_attempted": False,
            "reason": reason,
        },
    ).model_dump(mode="json")


def mcp_builtin_drawing_analysis_http_failure(*, status_code: int, payload: Any) -> dict[str, Any]:
    """A rejected read-only drawing MCP gateway call is confirmed failed."""

    return ToolResult(
        status="failed",
        data=payload,
        error_code=f"http_{status_code}",
        retryable=False,
        evidence={
            "adapter_contract": "mcp_builtin_drawing_analysis_v1",
            "action": _MCP_BUILTIN_DRAWING_ANALYSIS_ACTION,
            "gateway": _MCP_GATEWAY_PATH,
            "recipient_outcome": "rejected",
            "handler_dispatched": False,
            "http_status": status_code,
        },
    ).model_dump(mode="json")


def mcp_builtin_drawing_analysis_outcome_unknown(
    *, reason: str, status_code: int | None = None, payload: Any = None
) -> dict[str, Any]:
    """A dispatched drawing read has an unconfirmed outcome and is not replayed."""

    evidence: dict[str, Any] = {
        "adapter_contract": "mcp_builtin_drawing_analysis_v1",
        "action": _MCP_BUILTIN_DRAWING_ANALYSIS_ACTION,
        "gateway": _MCP_GATEWAY_PATH,
        "recipient_outcome": "unconfirmed",
        "dispatch_attempted": True,
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


def mcp_builtin_drawing_analysis_pre_dispatch_failure(*, reason: str) -> dict[str, Any]:
    """A local error proves that the drawing MCP gateway was not called."""

    return ToolResult(
        status="failed",
        error_code="mcp_dispatch_failed",
        retryable=False,
        evidence={
            "adapter_contract": "mcp_builtin_drawing_analysis_v1",
            "action": _MCP_BUILTIN_DRAWING_ANALYSIS_ACTION,
            "gateway": _MCP_GATEWAY_PATH,
            "recipient_outcome": "not_dispatched",
            "dispatch_attempted": False,
            "reason": reason,
        },
    ).model_dump(mode="json")


def async_job_http_failure(*, operation: str, status_code: int, payload: Any) -> dict[str, Any]:
    """A 4xx rejection means the reviewed async job was not accepted."""

    return ToolResult(
        status="failed",
        data=payload,
        error_code=f"http_{status_code}",
        retryable=False,
        evidence={
            "adapter_contract": "http_async_job_response_v1",
            "operation": operation,
            "http_status": status_code,
            "effect": "async_enqueue",
            "dispatch_attempted": True,
            "recipient_outcome": "rejected",
        },
    ).model_dump(mode="json")


def async_job_outcome_unknown(
    *,
    operation: str,
    reason: str,
    status_code: int | None = None,
    payload: Any = None,
) -> dict[str, Any]:
    """Stop after one dispatched enqueue whose recipient outcome is ambiguous."""

    evidence: dict[str, Any] = {
        "adapter_contract": "http_async_job_response_v1",
        "operation": operation,
        "effect": "async_enqueue",
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


def async_job_pre_dispatch_failure(*, operation: str, reason: str) -> dict[str, Any]:
    """Fail when the reviewed enqueue request never reached its recipient."""

    return ToolResult(
        status="failed",
        error_code="async_dispatch_failed",
        retryable=False,
        evidence={
            "adapter_contract": "http_async_job_response_v1",
            "operation": operation,
            "effect": "async_enqueue",
            "dispatch_attempted": False,
            "reason": reason,
        },
    ).model_dump(mode="json")


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
