"""Rarely used capabilities are not sent with every request (hermes-agent's
deferred tools): one `tools` entry names them, describe returns the schema and
call runs the real capability through every usual check."""

from __future__ import annotations

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig


@pytest.fixture
def harness(monkeypatch):
    config = BuiltinAgentConfig(
        enabled=True,
        model="mock",
        provider="ollama",
        backend_url="http://backend",
        ollama_url="http://ollama",
        memory_enabled=False,
        audit_enabled=False,
        context_compression_enabled=False,
        max_steps=8,
        deferred_capabilities=["normalization", "no-such-capability"],
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
    executed = []

    async def fake_execute_skill(skill, args, config, **kwargs):
        executed.append((skill.get("name"), dict(args)))
        return {"version": 1, "status": "succeeded", "data": {"items": [], "total": 0}}

    monkeypatch.setattr(agent_loop, "execute_skill", fake_execute_skill)
    seen = []
    replies = []

    async def fake_stream(messages, tools, system_prompt, config, on_token, **kw):
        seen.append({"messages": [dict(m) for m in messages], "tools": tools})
        reply = replies.pop(0)
        if isinstance(reply, str):
            await on_token(reply)
            return {"tool_calls": None}
        return reply

    monkeypatch.setattr(agent_loop, "_call_provider_streaming", fake_stream)

    async def collect(event):
        return None

    return agent_loop.AgentSession(collect), replies, seen, executed


def _names(tools):
    return [t["function"]["name"] for t in tools]


def _call(name, args):
    return {
        "tool_calls": [
            {
                "id": f"c{len(str(args))}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        ]
    }


@pytest.mark.asyncio
async def test_a_deferred_capability_is_named_not_sent(harness):
    session, replies, seen, executed = harness
    replies.extend(["Привет!", "ГОТОВО"])
    await session.on_user_message("Привет")
    names = _names(seen[0]["tools"])
    assert "normalization" not in names and "tools" in names
    meta = next(t for t in seen[0]["tools"] if t["function"]["name"] == "tools")
    assert "normalization —" in meta["function"]["description"]
    assert "no-such-capability" not in meta["function"]["description"]
    # The same list on every call: the head of the request stays cached.
    assert _names(seen[1]["tools"]) == names


@pytest.mark.asyncio
async def test_describe_then_call_runs_the_real_capability(harness):
    session, replies, seen, executed = harness
    replies.extend(
        [
            _call("tools", {"action": "describe", "capability": "normalization"}),
            _call(
                "tools",
                {
                    "action": "call",
                    "capability": "normalization",
                    "arguments": {"action": "list_rules"},
                },
            ),
            "Правил нет.",
            "ГОТОВО",
        ]
    )
    await session.on_user_message("Покажи правила нормализации")

    described = [m for m in seen[1]["messages"] if m.get("role") == "tool"][-1]["content"]
    assert "parameters" in described and "normalization" in described
    assert executed == [("normalization", {"action": "list_rules"})]


@pytest.mark.asyncio
async def test_a_call_to_a_sent_capability_through_tools_is_refused(harness):
    session, replies, seen, executed = harness
    replies.extend(
        [
            _call(
                "tools",
                {"action": "call", "capability": "invoices", "arguments": {"action": "list"}},
            ),
            "Не получилось.",
            "ГОТОВО",
        ]
    )
    await session.on_user_message("Покажи счета")
    assert executed == []
