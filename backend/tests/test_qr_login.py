"""Tests for QR-login: local session mint/verify/revocation, redeem, admin issue."""

from __future__ import annotations

import pytest

from app.auth import jwt as jwtmod
from app.db.models import User


# ── Fake Redis (decode_responses=True semantics) ──────────────────────────────


class _FakePipeline:
    def __init__(self, store: dict):
        self.store = store
        self.ops: list[tuple[str, str]] = []

    def get(self, k):
        self.ops.append(("get", k))
        return self

    def delete(self, k):
        self.ops.append(("delete", k))
        return self

    async def execute(self):
        out = []
        for op, k in self.ops:
            if op == "get":
                out.append(self.store.get(k))
            else:
                out.append(1 if self.store.pop(k, None) is not None else 0)
        return out


class FakeRedis:
    def __init__(self):
        self.store: dict = {}

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, ex=None):
        self.store[k] = v

    async def setex(self, k, ttl, v):
        self.store[k] = v

    async def delete(self, k):
        self.store.pop(k, None)

    async def incr(self, k):
        self.store[k] = int(self.store.get(k, 0)) + 1
        return self.store[k]

    def pipeline(self):
        return _FakePipeline(self.store)


@pytest.fixture
def fake_redis(monkeypatch):
    fr = FakeRedis()
    monkeypatch.setattr("app.utils.redis_client.get_async_redis", lambda: fr)
    return fr


@pytest.fixture
def no_user_checks(monkeypatch):
    """Skip DB/redis-backed active + role lookups in local-session verification."""
    async def _noop_active(sub):
        return None

    async def _no_role(sub):
        return None

    monkeypatch.setattr(jwtmod, "_assert_user_active", _noop_active)
    monkeypatch.setattr(jwtmod, "_db_role_for_sub", _no_role)


# ── mint / verify ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mint_and_verify_local_session(fake_redis, no_user_checks):
    token = jwtmod.mint_local_session(
        sub="u-1", email="u1@example.com", name="User One",
        preferred_username="u1", groups=["users"], ttl_seconds=600, session_epoch=0,
    )
    info = await jwtmod._verify_token(token)
    assert info.sub == "u-1"
    assert info.email == "u1@example.com"


@pytest.mark.asyncio
async def test_local_session_rejected_when_epoch_bumped(fake_redis, no_user_checks):
    token = jwtmod.mint_local_session(sub="u-2", ttl_seconds=600, session_epoch=0)
    # Admin revokes → epoch becomes 1, which is > the token's epoch 0.
    await jwtmod.revoke_user_sessions("u-2")
    with pytest.raises(Exception):
        await jwtmod._verify_token(token)


@pytest.mark.asyncio
async def test_local_session_rejected_when_jti_denylisted(fake_redis, no_user_checks):
    token = jwtmod.mint_local_session(sub="u-3", ttl_seconds=600, session_epoch=0)
    from jose import jwt as _jose
    jti = _jose.get_unverified_claims(token)["jti"]
    await jwtmod.revoke_session_jti(jti)
    with pytest.raises(Exception):
        await jwtmod._verify_token(token)


@pytest.mark.asyncio
async def test_tampered_local_session_rejected(no_user_checks):
    token = jwtmod.mint_local_session(sub="u-4", ttl_seconds=600)
    with pytest.raises(Exception):
        await jwtmod._verify_token(token + "x")


# ── redeem endpoint ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qr_redeem_bad_token(client, fake_redis):
    resp = await client.post("/api/auth/qr-login/redeem", json={"token": "missing"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_qr_redeem_sets_cookie(client, fake_redis, no_user_checks):
    token = jwtmod.mint_local_session(sub="u-5", email="u5@example.com", ttl_seconds=600)
    fake_redis.store["qrlogin:abc"] = token
    resp = await client.post("/api/auth/qr-login/redeem", json={"token": "abc"})
    assert resp.status_code == 200
    assert resp.json().get("ok") is True
    assert "access_token" in resp.cookies
    # single-use: the token is consumed
    assert "qrlogin:abc" not in fake_redis.store


# ── admin issues a QR-login for any user ──────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_login_qr_creates_token(client, db_session, fake_redis):
    db_session.add(
        User(sub="u-6", email="u6@example.com", name="Six",
             preferred_username="six", role="viewer", is_active=True)
    )
    await db_session.flush()

    resp = await client.post("/api/admin/users/u-6/login-qr")
    assert resp.status_code == 200
    data = resp.json()
    assert data["token"]
    assert data["expires_in"] > 0
    # the minted session JWT was stored under the qr token
    assert f"qrlogin:{data['token']}" in fake_redis.store


@pytest.mark.asyncio
async def test_admin_login_qr_unknown_user_404(client, fake_redis):
    resp = await client.post("/api/admin/users/nope/login-qr")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_revoke_sessions(client, db_session, fake_redis):
    db_session.add(
        User(sub="u-7", email="u7@example.com", name="Seven",
             preferred_username="seven", role="viewer", is_active=True)
    )
    await db_session.flush()
    resp = await client.post("/api/admin/users/u-7/revoke-sessions")
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True
