"""Tests for capability dispatcher (POST /api/agent/cap/{capability})."""

import pytest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_unknown_capability_returns_404(client: AsyncClient):
    r = await client.post("/api/agent/cap/nonexistent_cap", json={"action": "list"})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["error_code"] == "unknown_capability"
    assert "Unknown capability" in detail["message"]


@pytest.mark.asyncio
async def test_missing_action_returns_400(client: AsyncClient):
    r = await client.post("/api/agent/cap/documents", json={"some": "data"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error_code"] == "missing_action"
    assert "action" in detail["message"]


@pytest.mark.asyncio
async def test_unknown_action_returns_400(client: AsyncClient):
    r = await client.post(
        "/api/agent/cap/documents", json={"action": "nonexistent_action"}
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error_code"] == "unknown_action"
    assert "Unknown action" in detail["message"]
    assert isinstance(detail["available"], list) and detail["available"]


@pytest.mark.asyncio
async def test_dispatch_proxies_to_backend(client: AsyncClient):
    with patch(
        "app.api.capability_router._proxy",
        new=AsyncMock(return_value={"items": [], "total": 0}),
    ):
        r = await client.post("/api/agent/cap/documents", json={"action": "list"})
    assert r.status_code == 200
    assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_dispatch_flattens_filters_field(client: AsyncClient):
    proxy_mock = AsyncMock(return_value={"items": [], "total": 0})
    with patch("app.api.capability_router._proxy", new=proxy_mock):
        r = await client.post(
            "/api/agent/cap/documents",
            json={"action": "list", "filters": {"status": "approved"}},
        )
    assert r.status_code == 200
    # The flattened "status" key should be passed to _proxy
    _, kwargs_or_args = proxy_mock.call_args[0], proxy_mock.call_args
    call_body = proxy_mock.call_args[0][3]  # body is 4th positional arg
    assert call_body.get("status") == "approved"


@pytest.mark.asyncio
async def test_known_capabilities_available():
    """Dispatch table should have at least 10 named capabilities."""
    from app.api.capability_router import _DISPATCH

    assert len(_DISPATCH) >= 10


@pytest.mark.asyncio
async def test_each_capability_has_actions():
    """Every capability should have at least one action defined."""
    from app.api.capability_router import _DISPATCH

    for cap_name, actions in _DISPATCH.items():
        assert len(actions) > 0, f"Capability '{cap_name}' has no actions"


@pytest.mark.asyncio
async def test_dispatch_with_path_params(client: AsyncClient):
    doc_id = "00000000-0000-0000-0000-000000000001"
    proxy_mock = AsyncMock(return_value={"id": doc_id, "status": "approved"})
    with patch("app.api.capability_router._proxy", new=proxy_mock):
        r = await client.post(
            "/api/agent/cap/documents",
            json={"action": "get", "document_id": doc_id},
        )
    assert r.status_code == 200
    assert r.json()["id"] == doc_id


@pytest.mark.asyncio
async def test_risky_gate_action_requires_internal_approval(client: AsyncClient, monkeypatch):
    from app.ai.capability_manifest import CapabilityDefinition, CapabilityManifest
    from app.api import capability_router

    manifest = CapabilityManifest(
        capabilities=[CapabilityDefinition(name="invoices", gate_actions=["approve"])]
    )
    monkeypatch.setattr(capability_router, "load_capability_manifest", lambda: manifest)
    monkeypatch.setattr(capability_router.settings, "agent_service_key", "", raising=False)
    proxy_mock = AsyncMock(return_value={"ok": True})

    with patch("app.api.capability_router._proxy", new=proxy_mock):
        r = await client.post(
            "/api/agent/cap/invoices",
            json={
                "action": "approve",
                "invoice_id": "00000000-0000-0000-0000-000000000001",
            },
        )

    assert r.status_code == 423
    detail = r.json()["detail"]
    assert detail["error_code"] == "approval_required"
    assert detail["required_approval"] is True
    proxy_mock.assert_not_called()


@pytest.mark.asyncio
async def test_internal_approved_gate_action_dispatches(client: AsyncClient, monkeypatch):
    from app.ai.capability_manifest import CapabilityDefinition, CapabilityManifest
    from app.api import capability_router

    manifest = CapabilityManifest(
        capabilities=[CapabilityDefinition(name="invoices", gate_actions=["approve"])]
    )
    monkeypatch.setattr(capability_router, "load_capability_manifest", lambda: manifest)
    monkeypatch.setattr(capability_router.settings, "agent_service_key", "", raising=False)
    proxy_mock = AsyncMock(return_value={"ok": True})

    with patch("app.api.capability_router._proxy", new=proxy_mock):
        r = await client.post(
            "/api/agent/cap/invoices",
            json={
                "action": "approve",
                "invoice_id": "00000000-0000-0000-0000-000000000001",
            },
            headers={"X-Internal-Agent": "1", "X-Agent-Approval": "granted"},
        )

    assert r.status_code == 200
    assert r.json()["ok"] is True
    proxy_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_rejects_missing_path_params(client: AsyncClient):
    r = await client.post("/api/agent/cap/documents", json={"action": "get"})

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error_code"] == "missing_args"
    assert "document_id" in detail["missing"]


def test_runtime_contract_blocks_risky_action_without_gate(monkeypatch):
    from app.ai.capability_manifest import CapabilityDefinition, CapabilityManifest
    from app.api import capability_router

    manifest = CapabilityManifest(
        capabilities=[CapabilityDefinition(name="invoices", gate_actions=[])]
    )
    monkeypatch.setattr(capability_router, "load_capability_manifest", lambda: manifest)

    with pytest.raises(HTTPException) as exc:
        capability_router._validate_capability_contract(
            "invoices",
            "approve",
            ["invoice_id"],
            {"invoice_id": "invoice-1"},
        )

    assert getattr(exc.value, "status_code", None) == 503
    assert "missing from gate_actions" in str(getattr(exc.value, "detail", ""))


@pytest.mark.asyncio
async def test_dispatch_empty_body(client: AsyncClient):
    """Empty body should raise 400 because action is missing."""
    r = await client.post("/api/agent/cap/invoices", content=b"")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_dispatch_flattens_body_field(client: AsyncClient):
    proxy_mock = AsyncMock(return_value={"ok": True})
    with patch("app.api.capability_router._proxy", new=proxy_mock):
        r = await client.post(
            "/api/agent/cap/invoices",
            json={"action": "validate", "invoice_id": "abc-123", "body": {"amount": 100}},
        )
    assert r.status_code == 200
    call_body = proxy_mock.call_args[0][3]
    assert call_body.get("amount") == 100


@pytest.mark.asyncio
async def test_search_web_action_dispatches_to_web_search_adapter(client: AsyncClient):
    proxy_mock = AsyncMock(return_value={"results": [], "provider": "custom"})
    with patch("app.api.capability_router._proxy", new=proxy_mock):
        r = await client.post(
            "/api/agent/cap/search",
            json={"action": "web", "query": "каталог поставщика", "limit": 3},
        )

    assert r.status_code == 200
    method, path, _params, body, _base = proxy_mock.call_args[0]
    assert method == "POST"
    assert path == "/api/web-search/query"
    assert body["query"] == "каталог поставщика"


@pytest.mark.asyncio
async def test_memory_source_discover_action_dispatches(client: AsyncClient):
    proxy_mock = AsyncMock(return_value={"proposed": [], "provider": "custom"})
    with patch("app.api.capability_router._proxy", new=proxy_mock):
        r = await client.post(
            "/api/agent/cap/memory",
            json={
                "action": "source_discover",
                "supplier_name": "АКМЕ",
                "source_type": "supplier_catalog",
            },
        )

    assert r.status_code == 200
    method, path, _params, body, _base = proxy_mock.call_args[0]
    assert method == "POST"
    assert path == "/api/memory/sources/discover"
    assert body["supplier_name"] == "АКМЕ"


@pytest.mark.asyncio
async def test_learning_rule_reject_action_dispatches(client: AsyncClient):
    proxy_mock = AsyncMock(return_value={"status": "rejected"})
    with patch("app.api.capability_router._proxy", new=proxy_mock):
        r = await client.post(
            "/api/agent/cap/tech",
            json={
                "action": "learning_rule_reject",
                "entity_id": "rule-1",
                "rejected_by": "tester",
            },
        )

    assert r.status_code == 200
    method, path, params, body, _base = proxy_mock.call_args[0]
    assert method == "POST"
    assert path == "/api/technology/learning-rules/{entity_id}/reject"
    assert params == ["entity_id"]
    assert body["entity_id"] == "rule-1"
