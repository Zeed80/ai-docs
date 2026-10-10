"""E39: a cache of data never answers for another user.

The count fast path cached "Всего документов: N" under a key without the
user, so the number computed under one user's row rights was served to the
next for 15 s; the flow snapshot (unread personal mail among its counts) was
one global key for 20 s.
"""

from __future__ import annotations

import pytest

from app.ai import flow_awareness, result_cache, skill_cache
from app.ai.actor_context import cache_scope, set_acting_user


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, _ttl, value):
        self.data[key] = value


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(result_cache, "_redis", lambda: fake)
    monkeypatch.setattr(flow_awareness, "_redis", lambda: fake)
    yield fake
    set_acting_user(None)


def test_a_count_cached_for_alice_is_not_served_to_bob(redis):
    set_acting_user("alice")
    result_cache.cache_set("documents:list:", "Всего документов: 12.")
    assert result_cache.cache_get("documents:list:") == "Всего документов: 12."

    set_acting_user("bob")
    assert result_cache.cache_get("documents:list:") is None


def test_without_a_user_nothing_is_cached(redis):
    set_acting_user(None)
    result_cache.cache_set("documents:list:", "Всего документов: 12.")
    assert redis.data == {}
    assert result_cache.cache_get("documents:list:") is None
    # The service account is not a person either.
    set_acting_user("agent-service")
    assert cache_scope() is None


def test_the_scope_does_not_carry_the_raw_identity():
    set_acting_user("alice@example.com")
    try:
        assert "alice" not in cache_scope()
    finally:
        set_acting_user(None)


@pytest.mark.asyncio
async def test_the_flow_snapshot_is_per_user(redis, monkeypatch):
    calls: list[str | None] = []

    class _Response:
        status_code = 200

        def __init__(self, unread):
            self._unread = unread

        def json(self):
            return {"unread_emails": self._unread}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            from app.ai.actor_context import get_acting_user

            calls.append(get_acting_user())
            return _Response(7 if get_acting_user() == "alice" else 0)

    monkeypatch.setattr(flow_awareness.httpx, "AsyncClient", _Client)

    class _Config:
        backend_url = "http://backend"
        backend_timeout_seconds = 5

    set_acting_user("alice")
    assert (await flow_awareness.get_flow_snapshot(_Config()))["unread_emails"] == 7
    set_acting_user("bob")
    assert (await flow_awareness.get_flow_snapshot(_Config()))["unread_emails"] == 0


def test_skill_cache_keys_differ_by_user_and_need_one():
    set_acting_user(None)
    assert skill_cache._cache_key("invoices.list", {}) is None
    set_acting_user("alice")
    alice = skill_cache._cache_key("invoices.list", {})
    set_acting_user("bob")
    bob = skill_cache._cache_key("invoices.list", {})
    set_acting_user(None)
    assert alice and bob and alice != bob
