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
async def test_one_distinct_option_is_not_a_choice(client: AsyncClient):
    resp = await client.post(
        "/api/agent/clarify",
        json={"question": "Какой?", "options": [{"label": "A"}, {"label": "a"}]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_an_open_question_has_no_options(client: AsyncClient):
    """E52 №8: «какому поставщику писать?» had nothing to choose from, so the
    agent asked in plain text — a question the case could not see."""
    resp = await client.post("/api/agent/clarify", json={"question": "Какому поставщику написать?"})
    assert resp.status_code == 200
    assert resp.json()["options"] == []
    assert agent_loop.clarify_text(resp.json()) == "Какому поставщику написать?"


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


@pytest.mark.asyncio
async def test_an_at_reference_is_read_before_the_first_model_call(harness, monkeypatch):
    session, seen, events = harness
    executed = []

    async def fake_execute_skill(skill, args, config, **kwargs):
        executed.append(dict(args))
        if args.get("action") == "ask":
            return {"version": 1, "status": "succeeded", "data": dict(args)}
        return {
            "version": 1,
            "status": "succeeded",
            "data": {"id": args["invoice_id"], "invoice_number": "EV-4901"},
        }

    monkeypatch.setattr(agent_loop, "execute_skill", fake_execute_skill)
    invoice_id = "3b759084-86f5-4182-be2f-9613a2adac78"
    session.set_turn_references(
        [
            {"type": "invoice", "id": invoice_id},
            {"type": "invoice", "id": invoice_id},
            {"type": "x", "id": "1"},
        ]
    )
    await session.on_user_message("Что со счётом @счёт:EV-4901?")

    assert executed[0] == {"action": "get", "invoice_id": invoice_id}
    assert [a.get("action") for a in executed] == ["get", "ask"]
    first_call = seen[0]
    assert any(m.get("role") == "tool" and "EV-4901" in str(m.get("content")) for m in first_call)
