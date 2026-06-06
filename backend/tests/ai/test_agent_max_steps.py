"""Regression: the agent must never end a turn silently.

If the model keeps calling tools until the step budget is exhausted, the run
loop used to fall through and emit only ``done`` — leaving the user with no
reply (observed live on a multi-step 'check anomalies' query). The for/else +
_force_final_answer path now produces a final textual answer (a tool-less
summarisation call) or, failing that, an explicit fallback message.
"""

from __future__ import annotations

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig


@pytest.mark.asyncio
async def test_max_steps_emits_final_text(monkeypatch):
    config = BuiltinAgentConfig(
        enabled=True, agent_name="Света", model="mock", provider="ollama",
        backend_url="http://backend", ollama_url="http://ollama",
        memory_enabled=False, audit_enabled=False,
        context_compression_enabled=False, max_steps=3,
    )
    monkeypatch.setattr(agent_loop, "get_builtin_agent_config", lambda: config)

    async def _noop(*a, **k):
        return None

    for name in ("_log_action", "_init_mcp", "_append_memory_context",
                 "_inject_rating_hint", "_inject_learning_rules", "_remember_latest_turn"):
        monkeypatch.setattr(agent_loop.AgentSession, name, _noop, raising=False)

    async def fake_execute_skill(skill, args, config):  # noqa: A002
        return {"status": "ok"}

    monkeypatch.setattr(agent_loop, "_execute_skill", fake_execute_skill)

    # Model behaviour: keep calling a tool while tools are offered (never
    # answering), but when offered NO tools (the forced finalisation call),
    # produce a textual summary.
    async def fake_stream(messages, tools, system_prompt, config, on_token,
                          model_override=None, provider_override=None,
                          disable_thinking_override=None, on_thinking=None, **kw):
        if tools:
            return {"tool_calls": [{
                "type": "function",
                "function": {"name": "memory", "arguments": {"action": "search"}},
            }]}
        await on_token("Итог: по собранным данным аномалий не выявлено.")
        return {"tool_calls": None}

    monkeypatch.setattr(agent_loop, "_call_provider_streaming", fake_stream)

    events: list[dict] = []

    async def collect(evt: dict):
        events.append(evt)

    session = agent_loop.AgentSession(collect)
    await session.on_user_message("Проверь аномалии по счетам")

    texts = [e.get("content", "") for e in events if e.get("type") == "text"]
    assert any(t.strip() for t in texts), f"turn ended without a textual reply: {[e.get('type') for e in events]}"
    assert events[-1].get("type") == "done"
