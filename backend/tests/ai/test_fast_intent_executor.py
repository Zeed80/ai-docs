"""Integration test for the wired fast-path in AgentSession._try_fast_intent."""

from __future__ import annotations

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig


def _make_session(monkeypatch):
    config = BuiltinAgentConfig(
        enabled=True,
        model="mock",
        provider="ollama",
        backend_url="http://backend",
        ollama_url="http://ollama",
        memory_enabled=False,
        audit_enabled=False,
        context_compression_enabled=False,
    )
    monkeypatch.setattr(agent_loop, "get_builtin_agent_config", lambda: config)

    sent: list[dict] = []

    async def send(msg: dict):
        sent.append(msg)

    # Disable the result cache so these skill-path tests are deterministic
    # regardless of a real Redis being available (avoids cross-test cache bleed).
    from app.ai import result_cache
    monkeypatch.setattr(result_cache, "cache_get", lambda key: None)
    monkeypatch.setattr(result_cache, "cache_set", lambda key, value, ttl=15: None)

    session = agent_loop.AgentSession(send)
    session._config = config
    return session, sent


@pytest.mark.asyncio
async def test_fast_intent_counts_invoices_without_llm(monkeypatch):
    """A count question is answered by one direct skill call — no LLM streaming."""
    session, sent = _make_session(monkeypatch)
    session._skill_map = {"invoices": {"name": "invoices", "method": "POST", "path": "/api/agent/cap/invoices"}}

    executed: list[dict] = []

    async def fake_execute_skill(skill, args, config):
        executed.append({"name": skill["name"], "args": args})
        return {"total": 152, "items": [{"id": 1}]}

    monkeypatch.setattr(agent_loop, "_execute_skill", fake_execute_skill)

    # LLM must never be called on the fast path.
    async def fail_llm(*a, **k):
        raise AssertionError("LLM should not be called for a fast-path count query")

    monkeypatch.setattr(agent_loop, "_call_provider_streaming", fail_llm)

    session.messages.append({"role": "user", "content": "сколько счетов"})
    handled = await session._try_fast_intent()

    assert handled is True
    assert executed and executed[0]["name"] == "invoices"
    assert executed[0]["args"]["action"] == "list"
    text = next(m for m in sent if m["type"] == "text")
    assert "152" in text["content"]


@pytest.mark.asyncio
async def test_fast_intent_defers_on_skill_error(monkeypatch):
    """On a skill error the fast path defers (returns False) instead of lying."""
    session, sent = _make_session(monkeypatch)
    session._skill_map = {"invoices": {"name": "invoices", "method": "POST", "path": "/api/agent/cap/invoices"}}

    async def fake_execute_skill(skill, args, config):
        return {"error": "HTTP 500"}

    monkeypatch.setattr(agent_loop, "_execute_skill", fake_execute_skill)

    session.messages.append({"role": "user", "content": "сколько счетов"})
    handled = await session._try_fast_intent()

    assert handled is False
    assert not any(m["type"] == "text" for m in sent)


@pytest.mark.asyncio
async def test_fast_intent_skips_non_count_query(monkeypatch):
    """A non-count query is not fast-pathed."""
    session, _ = _make_session(monkeypatch)
    session._skill_map = {"invoices": {"name": "invoices", "method": "POST", "path": "/x"}}
    session.messages.append({"role": "user", "content": "покажи все счета таблицей"})
    assert await session._try_fast_intent() is False
