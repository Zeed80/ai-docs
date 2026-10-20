"""A turn that changed nothing is asked once whether it did what was asked.

E52 step 0: without the router's audit a reply "Проверю, есть ли счёт…" ended
the turn with no call at all. The model, not a keyword list, judges whether the
request asked for an action: the check is one extra call on the same history.
"""

from __future__ import annotations

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig


def _call(name: str, action: str) -> dict:
    return {
        "tool_calls": [
            {
                "id": f"call_{name}_{action}",
                "type": "function",
                "function": {"name": name, "arguments": {"action": action}},
            }
        ]
    }


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

    executed: list[tuple[str, str]] = []

    async def fake_execute_skill(skill, args, config, **kwargs):
        executed.append((skill.get("name", ""), str(args.get("action") or "")))
        return {"status": "ok", "id": "r1"}

    monkeypatch.setattr(agent_loop, "execute_skill", fake_execute_skill)

    state = {"replies": [], "seen": []}

    async def fake_stream(messages, tools, system_prompt, config, on_token, **kw):
        state["seen"].append([dict(m) for m in messages])
        reply = state["replies"].pop(0)
        if isinstance(reply, str):
            await on_token(reply)
            return {"tool_calls": None}
        return reply

    monkeypatch.setattr(agent_loop, "_call_provider_streaming", fake_stream)

    events: list[dict] = []

    async def collect(event: dict):
        events.append(event)

    session = agent_loop.AgentSession(collect)
    return session, state, events, executed


def _texts(events):
    return [e["content"] for e in events if e.get("type") == "text"]


def _has_check(messages) -> bool:
    return any(agent_loop.COMPLETION_CHECK_NOTE in str(m.get("content")) for m in messages)


@pytest.mark.asyncio
async def test_a_final_answer_stands_when_the_check_says_done(harness):
    session, state, events, _ = harness
    state["replies"] = ["Привет! Чем помочь?", "ГОТОВО"]

    await session.on_user_message("Привет")

    assert _texts(events) == ["Привет! Чем помочь?"]
    assert len(state["seen"]) == 2
    assert _has_check(state["seen"][1])
    # The check and its reply leave no trace in the history.
    assert not _has_check(session.messages)
    assert session.messages[-1] == {"role": "assistant", "content": "Привет! Чем помочь?"}


@pytest.mark.asyncio
async def test_an_announced_action_is_done_after_the_check(harness):
    session, state, events, executed = harness
    state["replies"] = [
        "Проверю, нет ли уже напоминания, и создам его.",
        _call("analytics", "calendar_create_reminder"),
        "Напоминание на пятницу 15:00 создано.",
    ]

    await session.on_user_message("Напомни в пятницу в 15:00 позвонить в Эвал-Никель")

    assert executed and executed[0][1] == "calendar_create_reminder"
    assert _texts(events)[-1] == "Напоминание на пятницу 15:00 создано."
    # Asked once: the second final answer follows a write and is not re-checked.
    assert sum(_has_check(seen) for seen in state["seen"]) == 1


@pytest.mark.asyncio
async def test_a_turn_that_changed_something_is_not_checked(harness):
    session, state, events, _ = harness
    state["replies"] = [
        _call("analytics", "calendar_create_reminder"),
        "Готово, напоминание создано.",
    ]

    await session.on_user_message("Напомни завтра в 10 позвонить поставщику")

    assert len(state["seen"]) == 2
    assert not any(_has_check(seen) for seen in state["seen"])
    assert _texts(events) == ["Готово, напоминание создано."]


@pytest.mark.asyncio
async def test_a_read_only_turn_is_checked_once(harness):
    session, state, events, _ = harness
    state["replies"] = [_call("invoices", "list"), "Счетов на проверке: 7.", "ГОТОВО"]

    await session.on_user_message("Сколько счетов на проверке?")

    assert _texts(events) == ["Счетов на проверке: 7."]
    assert _has_check(state["seen"][2])


@pytest.mark.asyncio
async def test_a_scoped_headless_session_is_not_checked(harness):
    session, state, events, _ = harness
    session.set_capability_scope({"email"})
    state["replies"] = ["Тема: оригинал счёта\n\nПросим прислать оригинал счёта."]

    await session.on_user_message("Напиши письмо поставщику")

    assert len(state["seen"]) == 1
    assert _texts(events) == ["Тема: оригинал счёта\n\nПросим прислать оригинал счёта."]


def test_a_scoped_session_sees_only_its_capabilities():
    session = agent_loop.AgentSession.__new__(agent_loop.AgentSession)
    session._tools = [
        {"type": "function", "function": {"name": name}}
        for name in ("email", "invoices", "warehouse", "procedures")
    ]
    session._capability_scope = None
    assert len(session._tools_for_turn()) == 4

    session.set_capability_scope({"email", "invoices"})
    names = {t["function"]["name"] for t in session._tools_for_turn()}
    assert names == {"email", "invoices", "procedures"}


@pytest.mark.asyncio
async def test_a_desktop_table_alone_does_not_count_as_done(harness):
    session, state, events, executed = harness
    state["replies"] = [
        _call("workspace", "spec_table"),
        "Таблица на Рабочем столе. Сейчас создам черновики писем.",
        _call("email", "draft"),
        "Черновики готовы.",
    ]

    await session.on_user_message("Подготовь письма поставщикам по мелким счетам")

    assert ("email", "draft") in executed
    assert sum(_has_check(seen) for seen in state["seen"]) == 1
    assert _texts(events)[-1] == "Черновики готовы."


def _call_args(name: str, args: dict) -> dict:
    return {
        "tool_calls": [
            {
                "id": f"call_{name}_{len(str(args))}_{id(args)}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        ]
    }


@pytest.mark.asyncio
async def test_the_same_read_is_not_repeated_in_a_turn(harness):
    # E52 2026-10-19: procurement.list_requests four times in a row.
    session, state, events, executed = harness
    read = {"action": "list_requests", "filters": {"limit": 50}}
    state["replies"] = [
        _call_args("procurement", dict(read)),
        _call_args("procurement", dict(read)),
        "Заявок нет.",
        "ГОТОВО",
    ]

    await session.on_user_message("Есть ли заявки?")

    assert executed == [("procurement", "list_requests")]
    repeated = [
        e
        for e in events
        if e.get("type") == "tool_result" and e["result"].get("status") == "already_read"
    ]
    assert len(repeated) == 1


@pytest.mark.asyncio
async def test_a_read_after_a_write_runs_again(harness):
    session, state, events, executed = harness
    read = {"action": "calendar_upcoming"}
    state["replies"] = [
        _call_args("analytics", dict(read)),
        _call("analytics", "calendar_create_reminder"),
        _call_args("analytics", dict(read)),
        "Напоминание создано.",
    ]

    await session.on_user_message("Напомни завтра в 10 позвонить поставщику")

    assert [a for _, a in executed] == [
        "calendar_upcoming",
        "calendar_create_reminder",
        "calendar_upcoming",
    ]


@pytest.mark.asyncio
async def test_a_guess_mark_in_the_body_leaves_the_journaled_call_intact(harness, monkeypatch):
    # The checkpoint journal holds the call by digest; stripping the mark in
    # place changed it and the confirmation checkpoint failed.
    session, state, events, executed = harness
    asked = []

    async def _decline(name, args):
        asked.append(args)
        return False

    monkeypatch.setattr(session, "_request_approval", _decline)
    update = {
        "action": "update",
        "supplier_id": "3ee9d1bd-af5d-4c82-868a-283c1d100d23",
        "body": {"contact_email": "new@eval-med.example", "target_match": "guess"},
    }
    reply = _call_args("suppliers", update)
    state["replies"] = [reply, "Изменение ждёт вашего подтверждения.", "ГОТОВО"]

    await session.on_user_message("Обнови e-mail Эвал-Стали")

    assert asked and "target_match" not in asked[0]["body"]
    sent = reply["tool_calls"][0]["function"]["arguments"]
    assert sent["body"]["target_match"] == "guess"
    assert executed == []
