"""A question with answer buttons ends the turn (hermes-agent's clarify).

The person answers with one click and the choice comes back as the next
message, as an exact value instead of free text to be parsed again.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig

QUESTION = {
    "action": "ask",
    "question": "Какой «Эвал-Медь» имеется в виду?",
    "options": [
        {"label": "ООО «Эвал-Медь», ИНН 7702000051", "value": "ООО «Эвал-Медь» (7702000051)"},
        {"label": "АО «Эвал-Медь», ИНН 7702000052"},
    ],
    "recommended": True,
}


@pytest.mark.asyncio
async def test_the_endpoint_normalises_options(client: AsyncClient):
    resp = await client.post(
        "/api/agent/clarify",
        json={
            "question": "  Какой  счёт? ",
            "options": [{"label": "EV-1"}, {"label": "ev-1"}, {"label": "EV-2", "value": "id-2"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "question": "Какой счёт?",
        "options": [{"label": "EV-1", "value": "EV-1"}, {"label": "EV-2", "value": "id-2"}],
        "recommended": False,
    }


@pytest.mark.asyncio
async def test_one_distinct_option_is_not_a_question(client: AsyncClient):
    resp = await client.post(
        "/api/agent/clarify",
        json={"question": "Какой?", "options": [{"label": "A"}, {"label": "a"}]},
    )
    assert resp.status_code == 422


def test_the_text_numbers_the_options_and_marks_the_recommended():
    text = agent_loop.clarify_text(
        {
            "question": "Какой?",
            "options": [{"label": "A", "value": "A"}, {"label": "B", "value": "B"}],
            "recommended": True,
        }
    )
    assert text == "Какой?\n\n1. A (рекомендую)\n2. B"


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

    async def fake_execute_skill(skill, args, config, **kwargs):
        options = [
            {"label": o["label"], "value": o.get("value") or o["label"]} for o in args["options"]
        ]
        return {
            "version": 1,
            "status": "succeeded",
            "data": {
                "question": args["question"],
                "options": options,
                "recommended": args.get("recommended", False),
            },
        }

    monkeypatch.setattr(agent_loop, "execute_skill", fake_execute_skill)
    seen: list = []
    replies = [
        {
            "tool_calls": [
                {
                    "id": "call_clarify",
                    "type": "function",
                    "function": {"name": "clarify", "arguments": dict(QUESTION)},
                }
            ]
        }
    ]

    async def fake_stream(messages, tools, system_prompt, config, on_token, **kw):
        seen.append(messages)
        return replies.pop(0)

    monkeypatch.setattr(agent_loop, "_call_provider_streaming", fake_stream)
    events: list[dict] = []

    async def collect(event: dict):
        events.append(event)

    return agent_loop.AgentSession(collect), seen, events


@pytest.mark.asyncio
async def test_a_clarify_call_ends_the_turn_with_the_question(harness):
    session, seen, events = harness

    await session.on_user_message("Напиши письмо Эвал-Меди о сроках поставки")

    # One model call: no completion check, no answer written over the question.
    assert len(seen) == 1
    kinds = [e["type"] for e in events if e["type"] in {"text", "clarify"}]
    assert kinds == ["text", "clarify"]
    clarify = next(e for e in events if e["type"] == "clarify")
    assert clarify["options"][0] == {
        "label": "ООО «Эвал-Медь», ИНН 7702000051",
        "value": "ООО «Эвал-Медь» (7702000051)",
    }
    assert clarify["recommended"] is True
    text = next(e for e in events if e["type"] == "text")["content"]
    assert text.startswith("Какой «Эвал-Медь» имеется в виду?")
    assert "1. ООО «Эвал-Медь», ИНН 7702000051 (рекомендую)" in text
    assert session.messages[-1] == {"role": "assistant", "content": text}
