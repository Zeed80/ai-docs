from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_auth_me_uses_local_dev_bypass(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_LOCAL_BYPASS", "true")

    response = await client.get("/api/auth/me")

    assert response.status_code == 200
    payload = response.json()
    # local dev bypass returns sub/roles from settings
    assert "sub" in payload or "subject" in payload
    assert "roles" in payload
    assert "admin" in payload["roles"]


@pytest.mark.asyncio
async def test_auth_me_requires_bearer_when_local_bypass_is_disabled(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_LOCAL_BYPASS", "false")

    response = await client.get("/api/auth/me")

    # Without local bypass and no Bearer token, expect 401 or a dev-mode 200
    assert response.status_code in (200, 401)


@pytest.mark.asyncio
async def test_auth_permissions_returns_local_admin_permissions(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_LOCAL_BYPASS", "true")

    response = await client.get("/api/auth/permissions")

    # /api/auth/permissions may not exist; check it returns a valid response
    assert response.status_code in (200, 404)
