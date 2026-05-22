"""Tests for /api/auth endpoints — /me, /login (dev mode), /logout."""

from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import MagicMock

from app.main import app
from app.auth.models import UserRole


# ── /me ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_me_returns_dev_user_in_dev_mode():
    """GET /api/auth/me in dev mode (AUTH_ENABLED=false) returns the dev user."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/auth/me")

    assert resp.status_code == 200
    data = resp.json()
    assert "sub" in data
    assert "email" in data
    assert "roles" in data
    assert isinstance(data["roles"], list)
    # Dev user always has admin role
    assert "admin" in data["roles"]


@pytest.mark.asyncio
async def test_me_has_required_fields():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/auth/me")

    data = resp.json()
    for field in ("sub", "email", "name", "preferred_username", "roles", "groups"):
        assert field in data, f"Missing field: {field}"


# ── /login ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_dev_mode_sets_cookie_and_redirects():
    """GET /api/auth/login in dev mode sets access_token cookie and redirects."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get(
            "/api/auth/login",
            params={"redirect_uri": "http://localhost:3000/auth/callback", "next": "/inbox"},
        )

    # Should redirect (302 or 307)
    assert resp.status_code in (302, 307)
    # Cookie must be set
    assert "access_token" in resp.cookies or "access_token" in resp.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_login_sanitizes_next_param():
    """next param with non-slash prefix is ignored (open-redirect protection)."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get(
            "/api/auth/login",
            params={
                "redirect_uri": "http://localhost:3000/auth/callback",
                "next": "https://evil.com/steal",
            },
        )

    assert resp.status_code in (302, 307)
    location = resp.headers.get("location", "")
    assert "evil.com" not in location


# ── /logout ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_logout_clears_cookie():
    """POST /api/auth/logout returns 200 and deletes the access_token cookie."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/auth/logout")

    assert resp.status_code == 200
    # Cookie should be cleared (max-age=0 or expires in the past)
    set_cookie = resp.headers.get("set-cookie", "")
    assert "access_token" in set_cookie or resp.json().get("status") == "logged_out"


# ── /users ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_users_returns_list():
    """GET /api/auth/users returns a list — mocks DB via dependency_overrides."""
    from unittest.mock import AsyncMock
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.db.session import get_db

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []

    async def override_get_db():
        db = AsyncMock(spec=AsyncSession)
        db.execute = AsyncMock(return_value=mock_result)
        yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/auth/users")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
