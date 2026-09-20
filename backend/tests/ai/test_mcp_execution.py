"""Chat MCP gateway routing and the process-level MCP tool registry."""

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


def test_mcp_approval_reuse_key_binds_action_and_arguments():
    first = {"action": "mcp_acme_ping", "arguments": {"target": "machine-1"}}
    changed_args = {"action": "mcp_acme_ping", "arguments": {"target": "machine-2"}}
    changed_action = {"action": "mcp_acme_stop", "arguments": {"target": "machine-1"}}

    first_key = agent_loop.AgentSession._approval_key("mcp", first)

    assert first_key != agent_loop.AgentSession._approval_key("mcp", changed_args)
    assert first_key != agent_loop.AgentSession._approval_key("mcp", changed_action)


@pytest.mark.asyncio
async def test_execute_skill_rejects_direct_mcp_handler():
    calls: list[dict] = []

    async def handler(args: dict) -> dict:
        calls.append(args)
        return {"ok": True, "echo": args}

    skill = {"name": "acme__ping", "_method": "mcp", "_handler": handler}
    result = await agent_loop.execute_skill(skill, {"target": "x"}, _config())

    assert calls == []
    assert result == {
        "version": 1,
        "status": "failed",
        "data": None,
        "error_code": "direct_mcp_handler_disabled",
        "retryable": False,
        "evidence": {"reason": "mcp_capability_gateway_required"},
        "checkpoint": None,
    }


@pytest.mark.asyncio
async def test_execute_skill_rejects_direct_builtin_mcp_handler():
    calls = 0

    async def handler(args: dict) -> dict:
        nonlocal calls
        calls += 1
        return {"drawing_id": args.get("drawing_id"), "status": "analyzed"}

    skill = {"name": "drawing_analysis_mcp", "_method": "builtin", "_handler": handler}
    result = await agent_loop.execute_skill(skill, {"drawing_id": "d1"}, _config())

    assert calls == 0
    assert result["version"] == 1
    assert result["status"] == "failed"
    assert result["error_code"] == "direct_mcp_handler_disabled"


@pytest.mark.asyncio
async def test_execute_skill_does_not_enter_failing_mcp_handler():
    calls = 0

    async def handler(args: dict) -> dict:
        nonlocal calls
        calls += 1
        raise RuntimeError("upstream MCP server unreachable")

    skill = {"name": "acme__ping", "_method": "mcp", "_handler": handler}
    result = await agent_loop.execute_skill(skill, {}, _config())

    assert calls == 0
    assert result["version"] == 1
    assert result["status"] == "failed"
    assert result["retryable"] is False


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

    assert result == {
        "version": 1,
        "status": "succeeded",
        "data": {"status": "ok"},
        "error_code": None,
        "retryable": False,
        "evidence": {"adapter_contract": "http_read_response_v1"},
        "checkpoint": None,
    }
    assert posted[0][0].endswith("/api/agent/cap/documents")


@pytest.mark.asyncio
async def test_execute_skill_routes_named_mcp_tool_through_gateway(monkeypatch):
    sent: dict = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):  # noqa: A002
            sent.update(url=url, json=json, headers=headers)
            return FakeResponse()

    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", FakeClient)
    skill = {
        "name": "mcp",
        "method": "POST",
        "path": "/api/agent/cap/mcp",
        "gate_actions": ["*"],
        "_mcp_action": "mcp_acme_ping",
    }
    result = await agent_loop.execute_skill(
        skill,
        {"target": "machine-1"},
        _config(),
        approval_granted=True,
    )

    expected_body = {
        "action": "mcp_acme_ping",
        "arguments": {"target": "machine-1"},
    }
    assert result == {"ok": True}  # result normalization belongs to E05.4.1
    assert sent["url"] == "http://backend/api/agent/cap/mcp"
    assert sent["json"] == expected_body
    assert sent["headers"]["X-Agent-Approval"] == "granted"
    assert sent["headers"]["X-Agent-Approval-Digest"] == (
        agent_loop.capability_args_digest(expected_body)
    )


@pytest.mark.asyncio
async def test_init_mcp_installs_gateway_descriptors_without_handlers(monkeypatch):
    async def direct_handler(args: dict) -> dict:
        raise AssertionError("chat must never receive this callable")

    async def fake_load_mcp_tools(servers):
        assert servers == [{"name": "acme", "transport": "http"}]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "mcp_acme_ping",
                    "description": "ping",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        handlers = {
            "mcp_acme_ping": {
                "name": "mcp_acme_ping",
                "_method": "mcp",
                "_handler": direct_handler,
            }
        }
        return tools, handlers

    monkeypatch.setattr("app.ai.mcp_client.load_mcp_tools", fake_load_mcp_tools)
    session = object.__new__(agent_loop.AgentSession)
    session._mcp_initialised = False
    session._config = _config().model_copy(
        update={"mcp_servers": [{"name": "acme", "transport": "http"}]}
    )
    session._tools = []
    session._skill_map = {}

    await session._init_mcp()

    assert [tool["function"]["name"] for tool in session._tools] == ["mcp_acme_ping"]
    assert session._skill_map["mcp_acme_ping"] == {
        "name": "mcp",
        "method": "POST",
        "path": "/api/agent/cap/mcp",
        "gate_actions": ["*"],
        "_mcp_action": "mcp_acme_ping",
    }


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
