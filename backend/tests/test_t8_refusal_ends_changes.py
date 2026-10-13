"""T8c: a refusal for lack of rights ends the changing part of the turn (E52)."""

import json

import httpx
import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.agent_loop import denied_writes
from tests.test_work_budget_tools import _claimed_context, _install_http

REFUSED = {
    "version": 1,
    "status": "failed",
    "error_code": "http_403",
    "evidence": {"http_status": 403},
}


def _turn(result):
    return [
        {"role": "user", "content": "Предложи задачу: сверять цены раз в месяц"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "p1",
                    "function": {
                        "name": "agent_control",
                        "arguments": {"action": "task_propose", "body": {"objective": "x"}},
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "p1", "content": json.dumps(result)},
    ]


def test_a_refused_change_is_listed():
    assert denied_writes(_turn(REFUSED)) == ["agent_control.task_propose"]
    assert denied_writes(_turn({"status": "succeeded", "data": {"id": "t"}})) == []


def test_a_refusal_in_an_earlier_turn_does_not_count():
    messages = _turn(REFUSED) + [
        {"role": "assistant", "content": "Нет прав"},
        {"role": "user", "content": "Тогда напомни мне"},
    ]
    assert denied_writes(messages) == []


@pytest.mark.asyncio
async def test_no_substitute_change_after_a_refusal(test_engine, monkeypatch):
    """E52: refused task_propose (403), the employee set reminders instead."""
    from app.ai import agent_config

    _, _, context = await _claimed_context(test_engine)
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"id": "r1"})], effects)
    config = BuiltinAgentConfig(approval_gates=[])
    monkeypatch.setattr(agent_config, "get_builtin_agent_config", lambda: config)

    async def send(event):
        return None

    session = agent_loop.AgentSession(send)
    session.set_work_budget_context(context)
    session._skill_map = {
        "analytics": {"name": "analytics", "method": "POST", "path": "/api/agent/cap/analytics"}
    }

    async def no_log(**kwargs):
        return None

    session._log_action = no_log
    session.messages = _turn(REFUSED)
    _, result, _ = await session._execute_single_tool(
        {
            "id": "r-1",
            "function": {
                "name": "analytics",
                "arguments": {
                    "action": "calendar_create_reminder",
                    "body": {"remind_at": "2026-12-01T09:00:00+03:00", "message": "Сверить цены"},
                },
            },
        },
        0,
    )
    assert result["error_code"] == "refused_earlier_in_turn"
    assert effects == []


def test_the_gateways_plain_refusal_counts_too():
    """E52: {"detail": "...administrator rights", "error": "HTTP 403"} was missed."""
    plain = {
        "detail": '{"detail":"This operation requires administrator rights"}',
        "error": "HTTP 403",
    }
    assert denied_writes(_turn(plain)) == ["agent_control.task_propose"]
    other = {"detail": "x", "error": "HTTP 422"}
    assert denied_writes(_turn(other)) == []


@pytest.mark.asyncio
async def test_a_number_where_an_id_is_due_is_refused_before_any_card(test_engine, monkeypatch):
    """E52: "Утверди счёт EV-8401" reached the owner as a card with invoice_id="EV-8401"."""
    from app.ai import agent_config

    _, _, context = await _claimed_context(test_engine)
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"ok": True})], effects)
    monkeypatch.setattr(
        agent_config, "get_builtin_agent_config", lambda: BuiltinAgentConfig(approval_gates=[])
    )

    async def send(event):
        return None

    session = agent_loop.AgentSession(send)
    session.set_work_budget_context(context)
    session._skill_map = {
        "invoices": {
            "name": "invoices",
            "method": "POST",
            "path": "/api/agent/cap/invoices",
            "gate_actions": ["approve"],
        }
    }

    async def no_log(**kwargs):
        return None

    session._log_action = no_log
    asked = []

    async def approve(skill_name, args):
        asked.append(args)
        return True

    session._request_approval = approve
    _, result, _ = await session._execute_single_tool(
        {
            "id": "a-1",
            "function": {
                "name": "invoices",
                "arguments": {"action": "approve", "invoice_id": "EV-8401"},
            },
        },
        0,
    )
    assert result["error_code"] == "bad_arguments"
    assert asked == [] and effects == []


def test_an_id_a_read_answered_404_for_is_found():
    from app.ai.agent_loop import not_found_in_turn

    inv = "3587b9c0-3f2b-4fc6-87cc-57cd819a31c9"
    gone = {
        "version": 1,
        "status": "failed",
        "error_code": "http_404",
        "data": {"detail": {"error": "HTTP 404"}},
    }
    messages = [
        {"role": "user", "content": "Утверди счёт EV-8401"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "g1",
                    "function": {
                        "name": "invoices",
                        "arguments": {"action": "get", "invoice_id": inv},
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "g1", "content": json.dumps(gone)},
    ]
    assert not_found_in_turn([inv], messages) == inv
    assert not_found_in_turn(["00000000-0000-4000-8000-000000000009"], messages) is None
