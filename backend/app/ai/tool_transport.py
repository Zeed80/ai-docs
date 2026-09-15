"""Retry policy from reviewed effects, never from HTTP verb or tool-name guesses."""

from app.ai.tool_catalog import TOOLS, get_tool


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
