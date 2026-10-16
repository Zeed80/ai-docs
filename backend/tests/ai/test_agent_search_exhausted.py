"""A turn stops searching once the searches only repeat or find nothing.

E52 run 5: with max_steps 40 the model called «suppliers.search Эвал-Медь»
25 times in a row and tried a missing page under 15 spellings; 37 of 360
turns ran 40+ model calls. At 10 steps the limit had been doing this job.
"""

from __future__ import annotations

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.agent_loop import FRUITLESS_ITERATIONS, fruitless_reads

FOUND = {"version": 1, "status": "succeeded", "data": {"results": [{"id": "s1", "name": "Медь"}]}}
EMPTY = {"version": 1, "status": "succeeded", "data": {"results": [], "total": 0}}
PAGE_404 = {"result": {"status": 404, "text": "404 Not Found", "url": "http://x/team.html"}}
SEARCH = {"action": "search", "query": "Эвал-Медь"}


def test_a_repeated_read_with_the_same_answer_is_fruitless():
    seen: set[str] = set()
    assert not fruitless_reads([("suppliers", SEARCH, FOUND)], seen)
    assert fruitless_reads([("suppliers", SEARCH, FOUND)], seen)


def test_an_empty_answer_or_a_missing_page_is_fruitless():
    seen: set[str] = set()
    assert fruitless_reads([("suppliers", {"action": "search", "query": "x"}, EMPTY)], seen)
    fetch = {"action": "browser_fetch", "target": "http://x/team.html"}
    assert fruitless_reads([("computer_use", fetch, PAGE_404)], seen)


def test_a_poll_whose_answer_changes_and_any_write_are_progress():
    seen: set[str] = set()
    status = {"action": "get", "supplier_id": "s1"}

    def answer(state):
        return {"version": 1, "status": "succeeded", "data": {"id": "s1", "state": state}}

    assert not fruitless_reads([("suppliers", status, answer("running"))], seen)
    assert not fruitless_reads([("suppliers", status, answer("done"))], seen)
    update = {"action": "update", "supplier_id": "s1", "body": {"inn": "1"}}
    assert not fruitless_reads([("suppliers", SEARCH, FOUND), ("suppliers", update, {})], seen)
    assert not fruitless_reads([], seen)


@pytest.mark.asyncio
async def test_the_turn_answers_after_three_fruitless_iterations(monkeypatch):
    config = BuiltinAgentConfig(
        enabled=True,
        agent_name="Света",
        model="mock",
        provider="ollama",
        backend_url="http://backend",
        ollama_url="http://ollama",
        memory_enabled=False,
        audit_enabled=False,
        context_compression_enabled=False,
        max_steps=40,
    )
    monkeypatch.setattr(agent_loop, "get_builtin_agent_config", lambda: config)

    async def _noop(*a, **k):
        return None

    for name in (
        "_log_action",
        "_init_mcp",
        "_append_memory_context",
        "_inject_rating_hint",
        "_inject_learning_rules",
        "_remember_latest_turn",
    ):
        monkeypatch.setattr(agent_loop.AgentSession, name, _noop, raising=False)

    async def fake_execute_skill(
        skill, args, config, *, approval_granted=False, budget_context=None
    ):
        return FOUND

    monkeypatch.setattr(agent_loop, "execute_skill", fake_execute_skill)
    tool_rounds = []

    async def fake_stream(messages, tools, system_prompt, config, on_token, **kw):
        if tools:
            tool_rounds.append(1)
            return {
                "tool_calls": [
                    {"type": "function", "function": {"name": "suppliers", "arguments": SEARCH}}
                ]
            }
        await on_token("Адреса Эвал-Меди в системе нет, пришлите его.")
        return {"tool_calls": None}

    monkeypatch.setattr(agent_loop, "_call_provider_streaming", fake_stream)
    events: list[dict] = []

    async def collect(evt: dict):
        events.append(evt)

    await agent_loop.AgentSession(collect).on_user_message("Напиши Эвал-Меди письмо")

    # The first search is news, the next three repeat it.
    assert len(tool_rounds) == 1 + FRUITLESS_ITERATIONS
    texts = [e.get("content", "") for e in events if e.get("type") == "text"]
    assert any("Эвал-Меди" in t for t in texts)
