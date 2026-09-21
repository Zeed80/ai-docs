"""Chat MCP gateway routing and the process-level MCP tool registry."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.tool_transport import (
    mcp_builtin_drawing_analysis_operation,
    mcp_builtin_tool_search_operation,
    serialize_mcp_builtin_drawing_analysis_response,
    serialize_mcp_builtin_tool_search_response,
)


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


@pytest.mark.parametrize(
    "skill,args,selected",
    [
        (
            {"method": "POST", "path": "/api/agent/cap/mcp"},
            {"action": "tool_search_mcp"},
            True,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/mcp/"},
            {"action": "tool_search_mcp"},
            False,
        ),
        (
            {"method": "GET", "path": "/api/agent/cap/mcp"},
            {"action": "tool_search_mcp"},
            False,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/mcp"},
            {"action": "drawing_analysis_mcp"},
            False,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/mcp"},
            {"action": "acme__search"},
            False,
        ),
    ],
)
def test_mcp_builtin_tool_search_resolver_is_exact(skill, args, selected):
    assert mcp_builtin_tool_search_operation(skill, args) is selected


@pytest.mark.parametrize(
    "skill,args,selected",
    [
        (
            {"method": "POST", "path": "/api/agent/cap/mcp"},
            {"action": "drawing_analysis_mcp", "arguments": {"drawing_id": "drawing-1"}},
            True,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/mcp"},
            {
                "action": "drawing_analysis_mcp",
                "arguments": {"drawing_id": "drawing-1", "reanalyze": False},
            },
            True,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/mcp"},
            {
                "action": "drawing_analysis_mcp",
                "arguments": {"drawing_id": "drawing-1", "reanalyze": True},
            },
            False,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/mcp"},
            {
                "action": "drawing_analysis_mcp",
                "arguments": {"drawing_id": "drawing-1", "reanalyze": "false"},
            },
            False,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/mcp"},
            {"action": "drawing_analysis_mcp", "arguments": {"drawing_id": " "}},
            False,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/mcp"},
            {"action": "drawing_analysis_mcp", "arguments": []},
            False,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/mcp/"},
            {"action": "drawing_analysis_mcp", "arguments": {"drawing_id": "drawing-1"}},
            False,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/mcp"},
            {"action": "acme__drawing", "arguments": {"drawing_id": "drawing-1"}},
            False,
        ),
    ],
)
def test_mcp_builtin_drawing_analysis_resolver_is_exact(skill, args, selected):
    assert mcp_builtin_drawing_analysis_operation(skill, args) is selected


def _drawing_payload(**overrides):
    payload = {
        "drawing": {
            "id": "drawing-1",
            "filename": "part.dxf",
            "format": "dxf",
            "status": "analyzed",
        },
        "features": [{"id": "feature-1", "dimensions": [], "surfaces": None, "gdt": []}],
        "total_features": 1,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "payload,arguments,expected_status,contract_error",
    [
        (_drawing_payload(), {"drawing_id": "drawing-1"}, "succeeded", None),
        (
            _drawing_payload(
                drawing={
                    "id": "other",
                    "filename": "part.dxf",
                    "format": "dxf",
                    "status": "analyzed",
                }
            ),
            {"drawing_id": "drawing-1"},
            "failed",
            "drawing_id_mismatch",
        ),
        (
            _drawing_payload(features=["bad"], total_features=1),
            {"drawing_id": "drawing-1"},
            "failed",
            "feature_not_mapping",
        ),
        (
            _drawing_payload(total_features=True),
            {"drawing_id": "drawing-1"},
            "failed",
            "total_features_not_integer",
        ),
        (
            _drawing_payload(total_features=0),
            {"drawing_id": "drawing-1"},
            "failed",
            "total_features_mismatch",
        ),
        (
            _drawing_payload(
                features=[{"id": "feature-1", "dimensions": "bad", "surfaces": [], "gdt": []}]
            ),
            {"drawing_id": "drawing-1"},
            "failed",
            "feature_dimensions_not_list_compatible",
        ),
        (
            _drawing_payload(features=[{"id": "feature-1"}]),
            {
                "drawing_id": "drawing-1",
                "include_dimensions": False,
                "include_surfaces": False,
                "include_gdt": False,
            },
            "succeeded",
            None,
        ),
        (
            _drawing_payload(error="recipient failed"),
            {"drawing_id": "drawing-1"},
            "failed",
            "recipient_domain_failure",
        ),
    ],
)
def test_mcp_builtin_drawing_analysis_adapter_accepts_only_reviewed_shape(
    payload, arguments, expected_status, contract_error
):
    result = serialize_mcp_builtin_drawing_analysis_response(payload, arguments=arguments)

    assert result["status"] == expected_status
    assert result["data"] == payload
    assert result["retryable"] is False
    assert result["evidence"]["adapter_contract"] == "mcp_builtin_drawing_analysis_v1"
    assert result["evidence"]["action"] == "drawing_analysis_mcp"
    if expected_status == "succeeded":
        assert result["evidence"]["recipient_outcome"] == "confirmed"
    else:
        assert result["error_code"] == "invalid_mcp_builtin_drawing_analysis_contract"
        assert result["evidence"]["contract_error"] == contract_error


def test_mcp_builtin_drawing_analysis_revalidates_versioned_success_and_preserves_nonterminal():
    arguments = {"drawing_id": "drawing-1"}
    succeeded = serialize_mcp_builtin_drawing_analysis_response(
        {"version": 1, "status": "succeeded", "data": _drawing_payload()}, arguments=arguments
    )
    malformed = serialize_mcp_builtin_drawing_analysis_response(
        {
            "version": 1,
            "status": "succeeded",
            "data": _drawing_payload(total_features=True),
        },
        arguments=arguments,
    )
    pending = {
        "version": 1,
        "status": "partial",
        "data": {"cursor": "next"},
        "error_code": "drawing_pending",
        "evidence": {"source": "recipient"},
        "checkpoint": {"cursor": "next"},
    }

    assert succeeded["status"] == "succeeded"
    assert malformed["status"] == "failed"
    assert malformed["data"]["data"]["total_features"] is True
    preserved = serialize_mcp_builtin_drawing_analysis_response(pending, arguments=arguments)
    assert preserved["status"] == "partial"
    assert preserved["data"] == pending["data"]
    assert preserved["checkpoint"] == pending["checkpoint"]


@pytest.mark.parametrize(
    "payload,expected_status,contract_error",
    [
        ({"results": [], "total": 0, "query": "drill"}, "succeeded", None),
        (
            {"results": [], "total": True, "query": "drill"},
            "failed",
            "total_not_nonnegative_integer",
        ),
        ({"results": {}, "total": 0, "query": "drill"}, "failed", "results_not_list"),
        ({"results": [], "total": -1, "query": "drill"}, "failed", "total_not_nonnegative_integer"),
        ({"results": [], "total": 0, "query": 42}, "failed", "query_not_string"),
        (
            {"results": [], "total": 0, "query": "drill", "error": "nope"},
            "failed",
            "recipient_domain_failure",
        ),
        (
            {"results": [], "total": 0, "query": "drill", "built": False},
            "failed",
            "recipient_domain_failure",
        ),
    ],
)
def test_mcp_builtin_tool_search_adapter_accepts_only_reviewed_shape(
    payload, expected_status, contract_error
):
    result = serialize_mcp_builtin_tool_search_response(payload)

    assert result["status"] == expected_status
    assert result["data"] == payload
    assert result["retryable"] is False
    assert result["evidence"]["adapter_contract"] == "mcp_builtin_tool_search_v1"
    assert result["evidence"]["action"] == "tool_search_mcp"
    assert result["evidence"]["gateway"] == "/api/agent/cap/mcp"
    if expected_status == "succeeded":
        assert result["evidence"]["recipient_outcome"] == "confirmed"
    else:
        assert result["error_code"] == "invalid_mcp_builtin_tool_search_contract"
        assert result["evidence"]["recipient_outcome"] == "confirmed_malformed"
        assert result["evidence"]["contract_error"] == contract_error


def test_mcp_builtin_tool_search_versioned_result_is_revalidated_and_nonterminal_preserved():
    valid_payload = {"results": [{"id": "tool-1"}], "total": 1, "query": "drill"}
    succeeded = serialize_mcp_builtin_tool_search_response(
        {"version": 1, "status": "succeeded", "data": valid_payload}
    )
    malformed = serialize_mcp_builtin_tool_search_response(
        {"version": 1, "status": "succeeded", "data": {"results": [], "total": True, "query": "d"}}
    )
    pending = {
        "version": 1,
        "status": "partial",
        "data": {"cursor": "next"},
        "error_code": "search_pending",
        "evidence": {"source": "recipient"},
        "checkpoint": {"cursor": "next"},
    }

    assert succeeded["status"] == "succeeded"
    assert succeeded["data"] == valid_payload
    assert succeeded["evidence"]["recipient_outcome"] == "confirmed"
    assert malformed["status"] == "failed"
    assert malformed["data"]["data"]["total"] is True
    assert malformed["evidence"]["contract_error"] == "total_not_nonnegative_integer"
    preserved = serialize_mcp_builtin_tool_search_response(pending)
    assert preserved["status"] == "partial"
    assert preserved["data"] == pending["data"]
    assert preserved["error_code"] == pending["error_code"]
    assert preserved["evidence"] == pending["evidence"]
    assert preserved["checkpoint"] == pending["checkpoint"]
    assert preserved["retryable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,expected_status",
    [
        (
            {"status_code": 200, "payload": {"results": [], "total": 0, "query": "drill"}},
            "succeeded",
        ),
        (
            {"status_code": 200, "payload": {"results": [], "total": True, "query": "drill"}},
            "failed",
        ),
        ({"status_code": 400, "payload": {"detail": "bad input"}}, "failed"),
        ({"status_code": 302, "payload": {"detail": "redirect"}}, "outcome_unknown"),
        ({"status_code": 502, "payload": {"error": "handler failed"}}, "outcome_unknown"),
    ],
)
async def test_execute_builtin_tool_search_has_one_attempt_and_explicit_outcomes(
    monkeypatch, response, expected_status
):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = type(
        "Response",
        (),
        {
            "status_code": response["status_code"],
            "json": staticmethod(lambda: response["payload"]),
            "text": "response text",
        },
    )()
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await agent_loop.execute_skill(
        {"name": "mcp", "method": "POST", "path": "/api/agent/cap/mcp"},
        {"action": "tool_search_mcp", "arguments": {"query": "drill"}},
        _config(),
    )

    assert result["status"] == expected_status
    assert result["retryable"] is False
    assert client.post.await_count == 1
    if expected_status == "failed" and response["status_code"] == 400:
        assert result["evidence"]["recipient_outcome"] == "rejected"
    if expected_status == "outcome_unknown":
        assert result["evidence"]["recipient_outcome"] == "unconfirmed"


@pytest.mark.asyncio
async def test_execute_builtin_tool_search_transport_is_unknown_once_and_preserves_gateway_body(
    monkeypatch,
):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.side_effect = agent_loop.httpx.ReadTimeout("timeout")
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    original_arguments = {"query": "drill", "limit": 2}
    result = await agent_loop.execute_skill(
        {
            "name": "mcp",
            "method": "POST",
            "path": "/api/agent/cap/mcp",
            "_mcp_action": "tool_search_mcp",
        },
        original_arguments,
        _config(),
        approval_granted=True,
    )

    expected_body = {"action": "tool_search_mcp", "arguments": original_arguments}
    assert result["status"] == "outcome_unknown"
    assert result["retryable"] is False
    assert client.post.await_count == 1
    assert client.post.await_args.kwargs["json"] == expected_body
    assert client.post.await_args.kwargs["headers"]["X-Agent-Approval-Digest"] == (
        agent_loop.capability_args_digest(expected_body)
    )


@pytest.mark.asyncio
async def test_execute_builtin_tool_search_pre_dispatch_failure_is_failed(monkeypatch):
    client_factory = AsyncMock()
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", client_factory)

    def fail_headers():
        raise RuntimeError("local header setup failed")

    monkeypatch.setattr(agent_loop, "internal_headers", fail_headers)

    result = await agent_loop.execute_skill(
        {"name": "mcp", "method": "POST", "path": "/api/agent/cap/mcp"},
        {"action": "tool_search_mcp", "arguments": {"query": "drill"}},
        _config(),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "mcp_dispatch_failed"
    assert result["retryable"] is False
    assert result["evidence"]["dispatch_attempted"] is False
    assert result["evidence"]["recipient_outcome"] == "not_dispatched"
    client_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,expected_status",
    [
        ({"status_code": 200, "payload": _drawing_payload()}, "succeeded"),
        ({"status_code": 200, "payload": _drawing_payload(total_features=True)}, "failed"),
        ({"status_code": 400, "payload": {"detail": "bad input"}}, "failed"),
        ({"status_code": 302, "payload": {"detail": "redirect"}}, "outcome_unknown"),
        ({"status_code": 502, "payload": {"error": "recipient failed"}}, "outcome_unknown"),
    ],
)
async def test_execute_builtin_drawing_analysis_has_one_attempt_and_explicit_outcomes(
    monkeypatch, response, expected_status
):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = type(
        "Response",
        (),
        {
            "status_code": response["status_code"],
            "json": staticmethod(lambda: response["payload"]),
            "text": "response text",
        },
    )()
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await agent_loop.execute_skill(
        {"name": "mcp", "method": "POST", "path": "/api/agent/cap/mcp"},
        {"action": "drawing_analysis_mcp", "arguments": {"drawing_id": "drawing-1"}},
        _config(),
    )

    assert result["status"] == expected_status
    assert result["retryable"] is False
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_execute_builtin_drawing_analysis_timeout_and_pre_dispatch_are_explicit(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.side_effect = agent_loop.httpx.ReadTimeout("timeout")
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})
    args = {"action": "drawing_analysis_mcp", "arguments": {"drawing_id": "drawing-1"}}

    timeout = await agent_loop.execute_skill(
        {"name": "mcp", "method": "POST", "path": "/api/agent/cap/mcp"}, args, _config()
    )

    assert timeout["status"] == "outcome_unknown"
    assert client.post.await_count == 1

    def fail_headers():
        raise RuntimeError("local header setup failed")

    monkeypatch.setattr(agent_loop, "internal_headers", fail_headers)
    pre_dispatch = await agent_loop.execute_skill(
        {"name": "mcp", "method": "POST", "path": "/api/agent/cap/mcp"}, args, _config()
    )

    assert pre_dispatch["status"] == "failed"
    assert pre_dispatch["error_code"] == "mcp_dispatch_failed"
    assert pre_dispatch["evidence"]["dispatch_attempted"] is False


@pytest.mark.asyncio
async def test_nonselected_mcp_action_retains_legacy_payload(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = type(
        "Response",
        (),
        {"status_code": 200, "json": staticmethod(lambda: {"ok": True}), "text": ""},
    )()
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await agent_loop.execute_skill(
        {"name": "mcp", "method": "POST", "path": "/api/agent/cap/mcp"},
        {
            "action": "drawing_analysis_mcp",
            "arguments": {"drawing_id": "d1", "reanalyze": True},
        },
        _config(),
    )

    assert result == {"ok": True}


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
