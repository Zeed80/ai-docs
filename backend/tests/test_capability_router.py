"""Tests for capability dispatcher (POST /api/agent/cap/{capability})."""

import pytest
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient


@pytest.mark.asyncio
async def test_unknown_capability_returns_404(client: AsyncClient):
    r = await client.post("/api/agent/cap/nonexistent_cap", json={"action": "list"})
    assert r.status_code == 404
    assert "Unknown capability" in r.json()["detail"]


@pytest.mark.asyncio
async def test_missing_action_returns_400(client: AsyncClient):
    r = await client.post("/api/agent/cap/documents", json={"some": "data"})
    assert r.status_code == 400
    assert "action" in r.json()["detail"]


@pytest.mark.asyncio
async def test_unknown_action_returns_400(client: AsyncClient):
    r = await client.post(
        "/api/agent/cap/documents", json={"action": "nonexistent_action"}
    )
    assert r.status_code == 400
    assert "Unknown action" in r.json()["detail"]
    assert "Available:" in r.json()["detail"]


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
