"""Admin Authentik integration endpoints: get/update/test, token never leaked."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_integration_defaults(client: AsyncClient):
    r = await client.get("/api/admin/integrations/authentik")
    assert r.status_code == 200
    d = r.json()
    assert "token_set" in d and "admin_url" in d and "external_url" in d
    # full token must never be returned
    assert "api_token" not in d


@pytest.mark.asyncio
async def test_set_token_and_external_url(client: AsyncClient):
    r = await client.put(
        "/api/admin/integrations/authentik",
        json={"api_token": "secret-token-abcd", "external_url": "https://auth.example.com/"},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["token_set"] is True
    assert d["token_hint"].endswith("abcd")
    assert "secret-token-abcd" not in str(d)  # only masked hint
    assert d["external_url"] == "https://auth.example.com"
    assert d["admin_url"] == "https://auth.example.com/if/admin/"

    # persisted across a fresh GET
    r2 = await client.get("/api/admin/integrations/authentik")
    assert r2.json()["token_set"] is True

    # clearing the token
    r3 = await client.put("/api/admin/integrations/authentik", json={"api_token": ""})
    assert r3.json()["token_set"] is False


@pytest.mark.asyncio
async def test_test_endpoint_without_token(client: AsyncClient):
    await client.put("/api/admin/integrations/authentik", json={"api_token": ""})
    r = await client.post("/api/admin/integrations/authentik/test")
    assert r.status_code == 200
    assert r.json()["ok"] is False
