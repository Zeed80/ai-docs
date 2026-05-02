"""Parallel tool execution helpers for AgentSession.

Read-only skills (list, get, search, analytics) are safe to run concurrently
via asyncio.gather.  Write/approval-gated skills must remain sequential.
"""

from __future__ import annotations

# Skills that are safe to execute in parallel (all are read-only).
PARALLEL_SAFE_PREFIXES: tuple[str, ...] = (
    "invoice__list",
    "invoice__get",
    "invoice__search",
    "invoice__analytics",
    "supplier__list",
    "supplier__get",
    "supplier__search",
    "document__list",
    "document__get",
    "document__search",
    "anomaly__list",
    "anomaly__get",
    "email__list",
    "email__get",
    "search__",
    "calendar__list",
    "table__list",
    "collection__list",
    "compare__get",
    "memory__search",
    "dashboard__",
    "procurement__list",
    "procurement__get",
    "payment__list",
    "payment__get",
    "warehouse__list",
    "warehouse__get",
    "bom__list",
    "bom__get",
    "technology__list",
    "technology__get",
    "ntd__list",
    "ntd__get",
)

# Skills that must NEVER run in parallel (write/approval-gated).
NEVER_PARALLEL: frozenset[str] = frozenset({
    "invoice__approve",
    "invoice__reject",
    "invoice__update",
    "email__send",
    "email__draft",
    "anomaly__resolve",
    "table__apply_diff",
    "approval__respond",
    "document__delete",
    "supplier__merge",
    "supplier__delete",
})


def _is_parallel_safe(tool_name: str) -> bool:
    if tool_name in NEVER_PARALLEL:
        return False
    return any(tool_name.startswith(p) for p in PARALLEL_SAFE_PREFIXES)


def should_parallelize(tool_calls: list[dict]) -> bool:
    """Return True when all tool calls in the batch are read-only and >= 2."""
    if len(tool_calls) < 2:
        return False
    return all(
        _is_parallel_safe(tc.get("function", {}).get("name", ""))
        for tc in tool_calls
    )
