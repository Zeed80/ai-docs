"""Session refresh: the hourly Authentik access token is renewed, not lost.

Live (2026-10-06): the access token lives an hour and nothing renewed it, so
every request turned 401 and the Strata panel reported it as "could not save
settings".
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_api
from app.auth.models import UserInfo
from app.config import settings
from app.main import app


def _jwt(exp_in: int) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'RS256'})}.{b64({'sub': 'u1', 'exp': int(time.time()) + exp_in})}.sig"


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    calls: list[str] = []

    async def exchange(refresh_token):
        calls.append(refresh_token)
        if refresh_token == "bad":
            raise HTTPException(status_code=401, detail="refresh_rejected")
        return {"access_token": _jwt(3600), "refresh_token": "rt-2", "expires_in": 3600}

    async def verify(token):
        return UserInfo(sub="u1", email="", name="", preferred_username="u1", roles=[], groups=[])

    monkeypatch.setattr(auth_api, "_exchange_refresh_token", exchange)
    monkeypatch.setattr("app.auth.jwt._verify_token", verify)
    return calls


async def _post(cookies: dict, **params):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as c:
        return await c.post("/api/auth/refresh", params=params)


@pytest.mark.asyncio
async def test_refresh_renews_the_access_cookie_and_rotates_the_refresh_cookie(auth_on):
    resp = await _post({"access_token": _jwt(-10), "refresh_token": "rt-1"})
    assert resp.status_code == 200
    assert resp.json()["refreshed"] is True
    cookies = resp.headers.get_list("set-cookie")
    access = next(c for c in cookies if c.startswith("access_token="))
    refresh = next(c for c in cookies if c.startswith("refresh_token="))
    assert "HttpOnly" in access and "Path=/" in access
    assert "rt-2" in refresh and "Path=/api/auth" in refresh and "SameSite=strict" in refresh
    assert auth_on == ["rt-1"]


@pytest.mark.asyncio
async def test_keepalive_does_nothing_while_the_token_is_fresh(auth_on):
    resp = await _post(
        {"access_token": _jwt(3000), "refresh_token": "rt-1"}, if_expiring_within=900
    )
    assert resp.status_code == 200
    assert resp.json()["refreshed"] is False
    assert auth_on == []


@pytest.mark.asyncio
async def test_no_or_rejected_refresh_token_is_a_401_that_clears_the_cookie(auth_on):
    resp = await _post({"access_token": _jwt(-10)})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "no_refresh_token"

    resp = await _post({"access_token": _jwt(-10), "refresh_token": "bad"})
    assert resp.status_code == 401
    assert any(
        c.startswith("refresh_token=") and "Max-Age=0" in c
        for c in resp.headers.get_list("set-cookie")
    )


@pytest.mark.asyncio
async def test_login_asks_for_offline_access(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)

    async def store(*_a, **_k):
        return None

    monkeypatch.setattr(auth_api, "_store_state", store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as c:
        resp = await c.get(
            "/api/auth/login",
            params={"redirect_uri": f"{settings.frontend_url}/auth/callback", "next": "/inbox"},
        )
    assert resp.status_code in (302, 307)
    assert "offline_access" in resp.headers["location"]


@pytest.mark.asyncio
async def test_concurrent_refreshes_spend_the_refresh_token_once(monkeypatch):
    import asyncio

    posts: list[dict] = []

    class Resp:
        status_code = 200

        def json(self):
            return {"access_token": "new", "refresh_token": "rt-next"}

    class Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def post(self, _url, data):
            posts.append(data)
            await asyncio.sleep(0.2)
            return Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    token = f"rt-{time.time()}"
    results = await asyncio.gather(*(auth_api._exchange_refresh_token(token) for _ in range(3)))
    assert len(posts) == 1
    assert all(r["refresh_token"] == "rt-next" for r in results)
