"""POST /api/agent/cap/mcp and GET /api/agent/cap/mcp/tools (Б17).

Calls the router functions directly with a minimal fake Request instead of
the `client` AsyncClient fixture — that fixture boots the full app against a
real Postgres connection, unavailable in this environment; dispatch_mcp only
reads request.headers and awaits request.json(), so a duck-typed stand-in is
enough to exercise the real policy/audit/dispatch code path.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.ai import mcp_capability
from app.api import capability_router


class FakeRequest:
    def __init__(self, json_body: dict, headers: dict | None = None):
        self._json_body = json_body
        self.headers = headers or {}

    async def json(self):
        return self._json_body


@pytest.fixture(autouse=True)
def _clean_mcp_cache():
    mcp_capability.reset_mcp_capability_cache()
    yield
    mcp_capability.reset_mcp_capability_cache()


def _stub_registry(monkeypatch, handlers: dict):
    """Bypass the real load_mcp_tools/subprocess machinery entirely."""

    async def _get_handler(name):
        return handlers.get(name)

    async def _list_names():
        return sorted(handlers.keys())

    monkeypatch.setattr(mcp_capability, "get_mcp_tool_handler", _get_handler)
    monkeypatch.setattr(mcp_capability, "list_mcp_tool_names", _list_names)


@pytest.mark.asyncio
async def test_dispatch_mcp_unknown_tool_returns_400(monkeypatch):
    _stub_registry(monkeypatch, {})
    request = FakeRequest({"action": "does_not_exist"})

    with pytest.raises(HTTPException) as exc_info:
        await capability_router.dispatch_mcp(request)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error_code"] == "unknown_action"


@pytest.mark.asyncio
async def test_dispatch_mcp_missing_action_returns_400(monkeypatch):
    _stub_registry(monkeypatch, {})
    request = FakeRequest({})

    with pytest.raises(HTTPException) as exc_info:
        await capability_router.dispatch_mcp(request)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error_code"] == "missing_action"


@pytest.mark.asyncio
async def test_dispatch_mcp_requires_approval_by_default(monkeypatch):
    """gate_actions: ["*"] in capabilities.yml — every MCP tool is gated,
    with no per-tool allowlist yet (A1/A2/Б17 principle: nothing structurally
    exempt from the approval check)."""
    called = False

    async def handler(args):
        nonlocal called
        called = True
        return {"ok": True}

    _stub_registry(monkeypatch, {"acme__ping": handler})
    monkeypatch.setattr(capability_router.settings, "agent_service_key", "", raising=False)
    request = FakeRequest({"action": "acme__ping", "arguments": {"x": 1}})

    with pytest.raises(HTTPException) as exc_info:
        await capability_router.dispatch_mcp(request)

    assert exc_info.value.status_code == 423
    assert exc_info.value.detail["error_code"] == "approval_required"
    assert called is False, "handler must not run before approval"


@pytest.mark.asyncio
async def test_dispatch_mcp_with_approval_invokes_handler(monkeypatch):
    received_args: dict = {}

    async def handler(args):
        received_args.update(args)
        return {"ok": True, "answer": 42}

    _stub_registry(monkeypatch, {"acme__ping": handler})
    monkeypatch.setattr(capability_router.settings, "agent_service_key", "", raising=False)
    request = FakeRequest(
        {"action": "acme__ping", "arguments": {"x": 1}},
        headers={"X-Internal-Agent": "1", "X-Agent-Approval": "granted"},
    )

    response = await capability_router.dispatch_mcp(request)

    assert received_args == {"x": 1}
    import json

    assert json.loads(response.body) == {"ok": True, "answer": 42}


@pytest.mark.asyncio
async def test_dispatch_mcp_handler_exception_returns_502(monkeypatch):
    async def handler(args):
        raise RuntimeError("MCP server crashed")

    _stub_registry(monkeypatch, {"acme__ping": handler})
    monkeypatch.setattr(capability_router.settings, "agent_service_key", "", raising=False)
    request = FakeRequest(
        {"action": "acme__ping"},
        headers={"X-Internal-Agent": "1", "X-Agent-Approval": "granted"},
    )

    response = await capability_router.dispatch_mcp(request)

    assert response.status_code == 502
    import json

    assert "MCP server crashed" in json.loads(response.body)["error"]


@pytest.mark.asyncio
async def test_list_mcp_tools_returns_registry_names(monkeypatch):
    _stub_registry(monkeypatch, {"acme__ping": None, "acme__pong": None})

    response = await capability_router.list_mcp_tools()

    import json

    assert json.loads(response.body) == {"tools": ["acme__ping", "acme__pong"]}


def test_mcp_is_exempt_from_dispatch_table_consistency_check():
    """mcp has no static _DISPATCH entry by design (Б17) — the fail-closed
    catalog check must treat it like "vault", not flag it as drift."""
    problems = capability_router.validate_capability_catalog()
    assert not any("'mcp'" in p for p in problems), problems
