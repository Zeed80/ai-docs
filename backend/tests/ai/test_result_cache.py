"""Tests for the short-TTL result cache + its use in the fast-intent path."""

from __future__ import annotations

import pytest

from app.ai import result_cache
from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig


def test_cache_graceful_without_redis(monkeypatch):
    """No Redis → get is a miss and set is a no-op (never raises)."""
    monkeypatch.setattr(result_cache, "_redis", lambda: None)
    assert result_cache.cache_get("k") is None
    result_cache.cache_set("k", "v")  # must not raise
    assert result_cache.cache_get("k") is None


def test_cache_roundtrip_with_fake_redis(monkeypatch):
    store: dict[str, str] = {}

    class _Fake:
        def get(self, k):
            return store.get(k)

        def setex(self, k, ttl, v):
            store[k] = v

    monkeypatch.setattr(result_cache, "_redis", lambda: _Fake())
    assert result_cache.cache_get("x") is None
    result_cache.cache_set("x", "answer")
    assert result_cache.cache_get("x") == "answer"


@pytest.mark.asyncio
async def test_fast_intent_cache_hit_skips_skill(monkeypatch):
    """A cached count answer is returned without calling the skill."""
    config = BuiltinAgentConfig(
        enabled=True, model="mock", provider="ollama",
        backend_url="http://backend", ollama_url="http://ollama",
        memory_enabled=False, audit_enabled=False, context_compression_enabled=False,
    )
    monkeypatch.setattr(agent_loop, "get_builtin_agent_config", lambda: config)

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    session = agent_loop.AgentSession(send)
    session._config = config
    session._skill_map = {"invoices": {"name": "invoices", "method": "POST", "path": "/x"}}

    # Pre-seed the cache (method re-imports cache_get from the module on call).
    from app.ai import result_cache as rc
    monkeypatch.setattr(rc, "cache_get", lambda k: "Всего счетов: 99.")

    async def fail_skill(*a, **k):
        raise AssertionError("skill must not run on cache hit")

    monkeypatch.setattr(agent_loop, "_execute_skill", fail_skill)

    session.messages.append({"role": "user", "content": "сколько счетов"})
    handled = await session._try_fast_intent()

    assert handled is True
    assert any(m["type"] == "text" and "99" in m["content"] for m in sent)
    assert not any(m["type"] == "tool_call" for m in sent)
