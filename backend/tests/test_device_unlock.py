"""Tests for biometric/PIN quick-login: enrol, redeem, revoke, status."""

from __future__ import annotations

import pytest

from app.db.models import DeviceUnlockCredential, User


class FakeRedis:
    """Minimal async Redis for session-epoch lookups during redeem."""

    def __init__(self):
        self.store: dict = {}

    async def get(self, k):
        return self.store.get(k)

    async def setex(self, k, ttl, v):
        self.store[k] = v

    async def incr(self, k):
        self.store[k] = int(self.store.get(k, 0)) + 1
        return self.store[k]


@pytest.fixture
def fake_redis(monkeypatch):
    fr = FakeRedis()
    monkeypatch.setattr("app.utils.redis_client.get_async_redis", lambda: fr)
    return fr


async def _seed_user(db, sub="dev-user", active=True):
    db.add(
        User(
            sub=sub,
            email=f"{sub}@example.com",
            name="Dev User",
            preferred_username=sub,
            role="viewer",
            is_active=active,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_enroll_returns_secret_and_persists(client, db_session):
    resp = await client.post("/api/auth/device-unlock/enroll", json={"method": "pin"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["handle"] and data["secret"]

    res = await db_session.execute(
        DeviceUnlockCredential.__table__.select().where(
            DeviceUnlockCredential.handle == data["handle"]
        )
    )
    row = res.first()
    assert row is not None
    # The raw secret is never stored — only its hash.
    assert data["secret"] not in row.secret_hash


@pytest.mark.asyncio
async def test_redeem_sets_cookie(client, db_session, fake_redis):
    await _seed_user(db_session)
    enroll = (
        await client.post("/api/auth/device-unlock/enroll", json={"method": "biometric"})
    ).json()

    resp = await client.post(
        "/api/auth/device-unlock/redeem",
        json={"handle": enroll["handle"], "secret": enroll["secret"]},
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True
    assert "access_token" in resp.cookies


@pytest.mark.asyncio
async def test_redeem_wrong_secret_rejected(client, db_session, fake_redis):
    await _seed_user(db_session)
    enroll = (
        await client.post("/api/auth/device-unlock/enroll", json={})
    ).json()

    resp = await client.post(
        "/api/auth/device-unlock/redeem",
        json={"handle": enroll["handle"], "secret": "wrong-secret"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_redeem_unknown_handle_rejected(client, db_session, fake_redis):
    resp = await client.post(
        "/api/auth/device-unlock/redeem",
        json={"handle": "nope", "secret": "nope"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_redeem_inactive_user_rejected(client, db_session, fake_redis):
    await _seed_user(db_session, active=False)
    enroll = (
        await client.post("/api/auth/device-unlock/enroll", json={})
    ).json()
    resp = await client.post(
        "/api/auth/device-unlock/redeem",
        json={"handle": enroll["handle"], "secret": enroll["secret"]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoke_kills_quick_login(client, db_session, fake_redis):
    await _seed_user(db_session)
    enroll = (
        await client.post("/api/auth/device-unlock/enroll", json={})
    ).json()

    revoke = await client.post(
        "/api/auth/device-unlock/revoke", json={"handle": enroll["handle"]}
    )
    assert revoke.status_code == 200
    assert revoke.json()["revoked"] == 1

    resp = await client.post(
        "/api/auth/device-unlock/redeem",
        json={"handle": enroll["handle"], "secret": enroll["secret"]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_status_counts_active(client, db_session):
    before = (await client.get("/api/auth/device-unlock/status")).json()["count"]
    await client.post("/api/auth/device-unlock/enroll", json={})
    after = (await client.get("/api/auth/device-unlock/status")).json()["count"]
    assert after == before + 1
