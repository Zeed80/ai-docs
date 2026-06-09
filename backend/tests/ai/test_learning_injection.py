"""Phase 4: behavioural learning-rule injection and the success signal."""

from __future__ import annotations

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.orchestrator_memory import TurnFeedback


# ── Success signal (semantic + mechanical) ──────────────────────────────────

@pytest.mark.parametrize(
    "audit,semantic,expected",
    [
        (True, True, True),
        (True, False, False),   # mechanically ok but semantically wrong → not a success
        (False, True, False),
        (False, False, False),
    ],
)
def test_turn_feedback_is_success(audit, semantic, expected):
    fb = TurnFeedback(
        intent_text="t", intent_category="data_analyst",
        skills_planned=[], skills_used=["invoices.list"],
        audit_passed=audit, semantic_passed=semantic,
    )
    assert fb.is_success is expected


# ── Behavioural rule injection ───────────────────────────────────────────────

class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeClient:
    def __init__(self, payload):
        self._p = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        return _FakeResp(self._p)


def _session_with_rules(monkeypatch, rules, user_text):
    config = BuiltinAgentConfig(
        enabled=True, model="mock", provider="ollama",
        backend_url="http://backend", ollama_url="http://ollama",
        memory_enabled=False, audit_enabled=False, context_compression_enabled=False,
    )
    monkeypatch.setattr(agent_loop, "get_builtin_agent_config", lambda: config)

    async def _noop_send(msg):
        return None

    session = agent_loop.AgentSession(_noop_send)
    session._config = config
    session.messages = [
        {"role": "system", "content": "BASE PROMPT"},
        {"role": "user", "content": user_text},
    ]
    payload = {"items": rules, "total": len(rules)}
    monkeypatch.setattr(
        agent_loop.httpx, "AsyncClient", lambda *a, **k: _FakeClient(payload)
    )
    return session


def _system_text(session) -> str:
    return next(m["content"] for m in session.messages if m["role"] == "system")


@pytest.mark.asyncio
async def test_behaviour_rule_injected_when_trigger_matches(monkeypatch):
    rules = [{
        "rule_type": "behavior",
        "field_name": "frez-count",
        "replacement_value": "При подсчёте фрез ищи и по синониму endmill.",
        "metadata": {"trigger_keywords": ["фрез"]},
    }]
    session = _session_with_rules(monkeypatch, rules, "сколько фрез на складе")
    await session._inject_learning_rules()
    text = _system_text(session)
    assert "поведения" in text.lower()
    assert "endmill" in text


@pytest.mark.asyncio
async def test_behaviour_rule_skipped_when_trigger_absent(monkeypatch):
    rules = [{
        "rule_type": "behavior",
        "field_name": "nakladnaya",
        "replacement_value": "Накладные проверяй по дате отгрузки.",
        "metadata": {"trigger_keywords": ["накладная"]},
    }]
    session = _session_with_rules(monkeypatch, rules, "сколько фрез на складе")
    await session._inject_learning_rules()
    assert "отгрузки" not in _system_text(session)


@pytest.mark.asyncio
async def test_global_behaviour_rule_always_injected(monkeypatch):
    rules = [{
        "rule_type": "behavior",
        "field_name": "tone",
        "replacement_value": "Всегда указывай источник суммы.",
        "metadata": {},  # no triggers → global
    }]
    session = _session_with_rules(monkeypatch, rules, "любой запрос")
    await session._inject_learning_rules()
    assert "источник суммы" in _system_text(session)


@pytest.mark.asyncio
async def test_nomenclature_rule_injected_in_its_section(monkeypatch):
    rules = [{
        "rule_type": "normalization_rule",
        "field_name": "invoices.extract",
        "replacement_value": "Болт М8 → Болт DIN933 M8.",
        "metadata": {},
    }]
    session = _session_with_rules(monkeypatch, rules, "извлеки позиции")
    await session._inject_learning_rules()
    text = _system_text(session)
    assert "номенклатуры" in text.lower()
    assert "DIN933" in text
