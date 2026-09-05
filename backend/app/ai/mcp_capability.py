"""Process-level MCP tool registry for the durable-runtime capability gateway.

``agent_loop.AgentSession._init_mcp`` loads MCP tools per chat session (its
own ``self._tools``/``self._skill_map``, torn down with the session). The
``mcp`` capability at ``/api/agent/cap/mcp`` (capability_router.py) is a
stateless HTTP endpoint shared by every WorkOrder step across every worker —
it needs exactly one long-lived set of MCP connections, loaded once and
reused, not re-spawned per request. This module is that cache.

See AGENT_SYSTEM_REMEDIATION_PLAN.md Б17: MCP tools reach WorkOrder plans
only through this capability (approval-gated by default, same policy/audit/
WorkToolCall path as every other capability) — never a direct call from
work_planning.py into mcp_client.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

logger = structlog.get_logger()

_lock = asyncio.Lock()
_handlers: dict[str, Callable[[dict], Awaitable[Any]]] | None = None
_tool_names: list[str] = []


async def _ensure_loaded() -> None:
    global _handlers, _tool_names
    if _handlers is not None:
        return
    async with _lock:
        if _handlers is not None:  # re-check: another task may have won the race
            return
        from app.ai.agent_config import get_builtin_agent_config
        from app.ai.mcp_client import load_mcp_tools

        servers = get_builtin_agent_config().mcp_servers or []
        try:
            # load_mcp_tools always includes the built-in MCP-shaped tools
            # (drawing_analysis_mcp, tool_search_mcp) even with an empty
            # server list — only externally-configured servers are optional.
            tools, handlers = await load_mcp_tools(servers)
        except Exception as exc:
            logger.warning("mcp_capability_load_failed", error=str(exc))
            _handlers, _tool_names = {}, []
            return
        _handlers = handlers
        _tool_names = sorted(
            t["function"]["name"]
            for t in tools
            if isinstance(t, dict)
            and isinstance(t.get("function"), dict)
            and t["function"].get("name")
        )
        logger.info("mcp_capability_loaded", count=len(_tool_names), servers=len(servers))


async def list_mcp_tool_names() -> list[str]:
    """Names of every MCP tool currently reachable through the capability gateway."""
    await _ensure_loaded()
    return list(_tool_names)


async def get_mcp_tool_handler(name: str) -> Callable[[dict], Awaitable[Any]] | None:
    await _ensure_loaded()
    return (_handlers or {}).get(name)


def reset_mcp_capability_cache() -> None:
    """Force a reload on next use — call after mcp_servers config changes, and
    from tests that need a clean cache between cases."""
    global _handlers, _tool_names
    _handlers, _tool_names = None, []
