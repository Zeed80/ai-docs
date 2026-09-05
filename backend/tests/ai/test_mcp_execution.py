"""execute_skill's MCP/builtin handler branch, and the mcp capability's
process-level tool registry (mcp_capability.py).

Regression guard: MCP-derived skill_map entries (``{"_method": "mcp"/
"builtin", "_handler": callable}``, no "method"/"path" keys — see
mcp_client.load_mcp_tools) previously hit ``skill["method"]`` unconditionally
in execute_skill and KeyError'd on the first real MCP tool call. The tool
schema and gate wiring worked; only invocation was broken, so nothing
exercised it until now.
"""

from __future__ import annotations

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig


def _config() -> BuiltinAgentConfig:
    return BuiltinAgentConfig(
        model="mock",
        backend_url="http://backend",
        ollama_url="http://ollama",
    )


@pytest.mark.asyncio
async def test_execute_skill_calls_mcp_handler_directly():
    calls: list[dict] = []

    async def handler(args: dict) -> dict:
        calls.append(args)
        return {"ok": True, "echo": args}

    skill = {"name": "acme__ping", "_method": "mcp", "_handler": handler}
    result = await agent_loop.execute_skill(skill, {"target": "x"}, _config())

    assert calls == [{"target": "x"}]
    assert result == {"ok": True, "echo": {"target": "x"}}


@pytest.mark.asyncio
async def test_execute_skill_calls_builtin_mcp_handler():
    async def handler(args: dict) -> dict:
        return {"drawing_id": args.get("drawing_id"), "status": "analyzed"}

    skill = {"name": "drawing_analysis_mcp", "_method": "builtin", "_handler": handler}
    result = await agent_loop.execute_skill(skill, {"drawing_id": "d1"}, _config())

    assert result == {"drawing_id": "d1", "status": "analyzed"}


@pytest.mark.asyncio
async def test_execute_skill_mcp_handler_exception_becomes_error_dict():
    async def handler(args: dict) -> dict:
        raise RuntimeError("upstream MCP server unreachable")

    skill = {"name": "acme__ping", "_method": "mcp", "_handler": handler}
    result = await agent_loop.execute_skill(skill, {}, _config())

    assert result == {"error": "upstream MCP server unreachable"}


@pytest.mark.asyncio
async def test_execute_skill_still_does_http_for_regular_skills(monkeypatch):
    """Non-MCP skills (method/path dicts) must be unaffected by the new branch."""
    posted: list[tuple[str, dict]] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "ok"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):  # noqa: A002
            posted.append((url, json or {}))
            return FakeResponse()

    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", FakeClient)
    skill = {"name": "documents", "method": "POST", "path": "/api/agent/cap/documents"}
    result = await agent_loop.execute_skill(skill, {"action": "list"}, _config())

    assert result == {"status": "ok"}
    assert posted[0][0].endswith("/api/agent/cap/documents")


class _FakeMCPClientModule:
    """Stand-in for app.ai.mcp_client — avoids spawning real subprocess/HTTP
    MCP transports in a unit test."""

    def __init__(self, tools, handlers):
        self._tools = tools
        self._handlers = handlers

    async def load_mcp_tools(self, server_configs):
        return self._tools, self._handlers


@pytest.mark.asyncio
async def test_mcp_capability_registry_caches_across_calls(monkeypatch):
    from app.ai import mcp_capability

    mcp_capability.reset_mcp_capability_cache()
    load_calls = {"n": 0}

    async def handler(args: dict) -> dict:
        return {"got": args}

    async def fake_load_mcp_tools(servers):
        load_calls["n"] += 1
        tools = [{"type": "function", "function": {"name": "acme__ping", "description": "x"}}]
        return tools, {"acme__ping": handler}

    monkeypatch.setattr("app.ai.mcp_client.load_mcp_tools", fake_load_mcp_tools)
    monkeypatch.setattr(
        "app.ai.agent_config.get_builtin_agent_config",
        lambda: _config(),
    )

    names1 = await mcp_capability.list_mcp_tool_names()
    names2 = await mcp_capability.list_mcp_tool_names()
    got_handler = await mcp_capability.get_mcp_tool_handler("acme__ping")

    assert names1 == ["acme__ping"] == names2
    assert load_calls["n"] == 1, "second call must hit the cache, not reload"
    assert got_handler is handler

    mcp_capability.reset_mcp_capability_cache()


@pytest.mark.asyncio
async def test_mcp_capability_unknown_tool_returns_none(monkeypatch):
    from app.ai import mcp_capability

    mcp_capability.reset_mcp_capability_cache()

    async def fake_load_mcp_tools(servers):
        return [], {}

    monkeypatch.setattr("app.ai.mcp_client.load_mcp_tools", fake_load_mcp_tools)
    monkeypatch.setattr(
        "app.ai.agent_config.get_builtin_agent_config",
        lambda: _config(),
    )

    assert await mcp_capability.get_mcp_tool_handler("does_not_exist") is None
    mcp_capability.reset_mcp_capability_cache()
