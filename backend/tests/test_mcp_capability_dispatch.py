"""POST /api/agent/cap/mcp and GET /api/agent/cap/mcp/tools (Б17).

Calls the router functions directly with a minimal fake Request instead of
the `client` AsyncClient fixture — that fixture boots the full app against a
real Postgres connection, unavailable in this environment; dispatch_mcp only
reads request.headers and awaits request.json(), so a duck-typed stand-in is
enough to exercise the real policy/audit/dispatch code path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient

from app.ai import mcp_capability
from app.ai.agent_loop import capability_args_digest
from app.api import capability_router
from app.auth.jwt import get_current_user
from app.auth.models import UserRole


class FakeRequest:
    def __init__(self, json_body: dict, headers: dict | None = None):
        self._json_body = json_body
        self.headers = headers or {}
        self.state = SimpleNamespace(execution_user=None, delegation_id=None)

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


def _approved_mcp_headers(body: dict) -> dict[str, str]:
    return {
        "X-Internal-Agent": "1",
        "X-Agent-Approval": "granted",
        "X-Agent-Approval-Digest": capability_args_digest(body),
    }


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
    calls = 0

    async def handler(args):
        nonlocal calls
        calls += 1
        received_args.update(args)
        return {"ok": True, "answer": 42}

    _stub_registry(monkeypatch, {"acme__ping": handler})
    audit = AsyncMock()
    monkeypatch.setattr(capability_router, "_audit_tool_call", audit)
    monkeypatch.setattr(capability_router.settings, "agent_service_key", "", raising=False)
    raw_body = {"action": "acme__ping", "arguments": {"x": 1}}
    request = FakeRequest(
        raw_body,
        headers=_approved_mcp_headers(raw_body),
    )

    response = await capability_router.dispatch_mcp(request)

    assert received_args == {"x": 1}
    assert calls == 1
    audit.assert_awaited_once_with("mcp", "acme__ping", None, request)
    import json

    assert json.loads(response.body) == {"ok": True, "answer": 42}


@pytest.mark.asyncio
async def test_dispatch_mcp_handler_exception_returns_502(monkeypatch):
    async def handler(args):
        raise RuntimeError("MCP server crashed")

    _stub_registry(monkeypatch, {"acme__ping": handler})
    audit = AsyncMock()
    monkeypatch.setattr(capability_router, "_audit_tool_call", audit)
    monkeypatch.setattr(capability_router.settings, "agent_service_key", "", raising=False)
    raw_body = {"action": "acme__ping"}
    request = FakeRequest(
        raw_body,
        headers=_approved_mcp_headers(raw_body),
    )

    response = await capability_router.dispatch_mcp(request)

    assert response.status_code == 502
    audit.assert_awaited_once_with("mcp", "acme__ping", None, request)
    import json

    assert "MCP server crashed" in json.loads(response.body)["error"]


@pytest.mark.asyncio
async def test_dispatch_mcp_rejects_approval_for_different_arguments(monkeypatch):
    calls = 0

    async def handler(args):
        nonlocal calls
        calls += 1
        return {"ok": True}

    _stub_registry(monkeypatch, {"acme__ping": handler})
    monkeypatch.setattr(capability_router.settings, "agent_service_key", "", raising=False)
    approved_body = {"action": "acme__ping", "arguments": {"x": 1}}
    request = FakeRequest(
        {"action": "acme__ping", "arguments": {"x": 2}},
        headers=_approved_mcp_headers(approved_body),
    )

    with pytest.raises(HTTPException) as exc_info:
        await capability_router.dispatch_mcp(request)

    assert exc_info.value.status_code == 423
    assert calls == 0


@pytest.mark.asyncio
async def test_list_mcp_tools_returns_registry_names(monkeypatch):
    _stub_registry(monkeypatch, {"acme__ping": None, "acme__pong": None})

    response = await capability_router.list_mcp_tools()

    import json

    assert json.loads(response.body) == {"tools": ["acme__ping", "acme__pong"]}


@pytest.mark.asyncio
async def test_builtin_mcp_gateway_reaches_protected_api_recipients(monkeypatch):
    """Exercise the real gateway and built-in handlers against ASGI routes.

    The client factory is replaced only with an ASGI transport, so this
    verifies the exact ``/api`` paths plus service authentication without a
    live socket or database.
    """
    from app.ai import mcp_client

    app = FastAPI()
    app.include_router(capability_router.router, prefix="/api/agent")
    seen: list[tuple[str, str, str | None]] = []

    async def bind_test_owner(request: Request) -> None:
        # The production dependency obtains this human from the signed service
        # context. The isolated test has no database-backed owner to resolve.
        request.state.execution_user = SimpleNamespace(sub="test-owner", roles=[UserRole.admin])

    app.dependency_overrides[capability_router._bind_actor] = bind_test_owner

    protected = [Depends(get_current_user)]

    @app.get("/api/tool-catalog/search", dependencies=protected)
    async def search_recipient(request: Request, query: str, page_size: int):
        seen.append(("search", request.headers.get("x-internal-agent", ""), query))
        assert page_size == 7
        return {"items": [{"id": "tool-1"}], "total": 1}

    @app.get("/api/drawings/{drawing_id}", dependencies=protected)
    async def drawing_recipient(request: Request, drawing_id: str):
        seen.append(("drawing", request.headers.get("x-internal-agent", ""), drawing_id))
        return {
            "id": drawing_id,
            "filename": "part.dxf",
            "format": "dxf",
            "status": "analyzed",
            "title_block": {"number": "A-1"},
        }

    @app.get("/api/drawings/{drawing_id}/features", dependencies=protected)
    async def drawing_features_recipient(request: Request, drawing_id: str):
        seen.append(("features", request.headers.get("x-internal-agent", ""), drawing_id))
        return [{"id": "feature-1", "feature_type": "hole", "dimensions": []}]

    def asgi_builtin_client(*, timeout: float):
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://builtin.test",
            headers=mcp_client._builtin_backend_headers(),
        )

    async def get_handler(name: str):
        return {
            "tool_search_mcp": mcp_client._handle_tool_search_mcp,
            "drawing_analysis_mcp": mcp_client._handle_drawing_analysis_mcp,
        }.get(name)

    monkeypatch.setattr(mcp_client, "_builtin_backend_client", asgi_builtin_client)
    monkeypatch.setattr(mcp_capability, "get_mcp_tool_handler", get_handler)
    monkeypatch.setattr(mcp_capability, "list_mcp_tool_names", AsyncMock(return_value=[]))
    monkeypatch.setattr(capability_router, "_audit_tool_call", AsyncMock())
    monkeypatch.setattr(capability_router.settings, "agent_service_key", "mcp-test-key")
    monkeypatch.setattr("app.auth.jwt.settings.auth_enabled", True)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        search_body = {"action": "tool_search_mcp", "arguments": {"query": "drill", "limit": 7}}
        search_response = await client.post(
            "/api/agent/cap/mcp",
            json=search_body,
            headers={**_approved_mcp_headers(search_body), "X-API-Key": "mcp-test-key"},
        )
        drawing_id = "00000000-0000-0000-0000-000000000001"
        drawing_body = {"action": "drawing_analysis_mcp", "arguments": {"drawing_id": drawing_id}}
        drawing_response = await client.post(
            "/api/agent/cap/mcp",
            json=drawing_body,
            headers={**_approved_mcp_headers(drawing_body), "X-API-Key": "mcp-test-key"},
        )

    assert search_response.status_code == 200
    assert search_response.json() == {"results": [{"id": "tool-1"}], "total": 1, "query": "drill"}
    assert drawing_response.status_code == 200
    assert drawing_response.json()["drawing"]["id"] == drawing_id
    assert drawing_response.json()["total_features"] == 1
    assert seen == [
        ("search", "1", "drill"),
        ("drawing", "1", drawing_id),
        ("features", "1", drawing_id),
    ]


def test_builtin_api_url_uses_configured_backend_service(monkeypatch):
    """Docker built-ins must not resolve their own API recipient as localhost."""
    from app.ai import agent_config, mcp_client

    monkeypatch.setattr(
        agent_config,
        "get_builtin_agent_config",
        lambda: SimpleNamespace(backend_url="http://backend:8000/"),
    )

    assert mcp_client._builtin_api_url("/tool-catalog/search") == (
        "http://backend:8000/api/tool-catalog/search"
    )


@pytest.mark.asyncio
async def test_builtin_drawing_reanalyze_fails_closed_before_snapshot(monkeypatch):
    """A failed restart is not converted into a successful stale analysis read."""
    from app.ai import mcp_client

    app = FastAPI()
    app.include_router(capability_router.router, prefix="/api/agent")
    snapshot_requested = False

    @app.post("/api/drawings/{drawing_id}/reanalyze")
    async def reanalyze_recipient(drawing_id: str):
        return __import__("fastapi").responses.JSONResponse(
            status_code=503, content={"detail": "queue unavailable"}
        )

    @app.get("/api/drawings/{drawing_id}")
    async def drawing_recipient(drawing_id: str):
        nonlocal snapshot_requested
        snapshot_requested = True
        return {"id": drawing_id}

    def asgi_builtin_client(*, timeout: float):
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://builtin.test",
            headers=mcp_client._builtin_backend_headers(),
        )

    async def get_handler(name: str):
        return mcp_client._handle_drawing_analysis_mcp if name == "drawing_analysis_mcp" else None

    monkeypatch.setattr(mcp_client, "_builtin_backend_client", asgi_builtin_client)
    monkeypatch.setattr(mcp_capability, "get_mcp_tool_handler", get_handler)
    monkeypatch.setattr(capability_router, "_audit_tool_call", AsyncMock())
    monkeypatch.setattr(capability_router.settings, "agent_service_key", "", raising=False)
    body = {
        "action": "drawing_analysis_mcp",
        "arguments": {"drawing_id": "00000000-0000-0000-0000-000000000001", "reanalyze": True},
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        response = await client.post(
            "/api/agent/cap/mcp", json=body, headers=_approved_mcp_headers(body)
        )

    assert response.status_code == 502
    assert snapshot_requested is False


def test_mcp_is_exempt_from_dispatch_table_consistency_check():
    """mcp has no static _DISPATCH entry by design (Б17) — the fail-closed
    catalog check must treat it like "vault", not flag it as drift."""
    problems = capability_router.validate_capability_catalog()
    assert not any("'mcp'" in p for p in problems), problems
