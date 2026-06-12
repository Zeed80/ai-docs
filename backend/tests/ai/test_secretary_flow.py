"""Secretary front-agent: flow-status questions answered directly (0 LLM)."""

from __future__ import annotations

import pytest

from app.ai import flow_awareness
from app.ai import orchestrator as orchestrator_module
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.orchestrator import AgentOrchestrator, _is_secretary_query


@pytest.mark.parametrize(
    "text,expected",
    [
        ("что требует внимания?", True),
        ("дай сводку по документам", True),
        ("статус потока документооборота", True),
        ("что просрочено", True),
        ("покажи все счета таблицей", False),
        ("сколько фрез на складе", False),
        ("утверди счёт 42", False),
    ],
)
def test_is_secretary_query(text, expected):
    assert _is_secretary_query(text) is expected


def test_format_snapshot_contains_counts():
    today = {
        "pending_approvals": 3,
        "documents_needs_review": 5,
        "open_anomalies": 2,
        "quarantine_count": 1,
        "unread_emails": 7,
    }
    text = flow_awareness._format_snapshot(today, overdue=4)
    assert "<flow-context>" in text and "</flow-context>" in text
    assert "approval): 3" in text
    assert "needs_review): 5" in text
    assert "аномалии: 2" in text
    assert "Просроченные платежи: 4" in text


def test_human_summary_prioritises_urgent():
    snapshot = {
        "pending_approvals": 3,
        "documents_needs_review": 5,
        "open_anomalies": 2,
        "quarantine_count": 0,
        "unread_emails": 7,
        "overdue_payments": 4,
    }
    text = flow_awareness.format_flow_summary_human(snapshot)
    assert "Просроченные платежи: 4" in text
    assert "Ожидают согласования: 3" in text
    assert "Документы на проверке: 5" in text
    # Urgent block comes before the regular block.
    assert text.index("Просроченные платежи") < text.index("Документы на проверке")


def test_human_summary_all_clear():
    text = flow_awareness.format_flow_summary_human({})
    assert "спокойно" in text.lower()


class _Executor:
    """Minimal executor double for the direct path."""

    def __init__(self):
        self.external_turns: list[tuple[str, str]] = []
        self.user_messages: list[str] = []

    def hydrate_history(self, m):
        return None

    def recent_dialogue(self, limit=20):
        return []

    def inject_orchestrator_hint(self, h):
        return None

    def set_role_context(self, rc):
        return None

    def set_response_budget(self, n):
        return None

    def set_model_override(self, model):
        return None

    def set_active_role(self, role):
        return None

    def record_external_turn(self, user_text, assistant_text):
        self.external_turns.append((user_text, assistant_text))

    async def on_approval(self, a):
        return None

    async def on_user_message(self, content):
        self.user_messages.append(content)


@pytest.mark.asyncio
async def test_secretary_answers_flow_status_directly(monkeypatch):
    """Flow-status question → direct answer from live data: no executor turn."""
    config = BuiltinAgentConfig(
        department_enabled=True, audit_enabled=True,
        model="mock", backend_url="http://backend", ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    async def fake_snapshot(cfg, **k):
        return {
            "pending_approvals": 3,
            "documents_needs_review": 5,
            "open_anomalies": 2,
            "quarantine_count": 1,
            "unread_emails": 7,
            "overdue_payments": 0,
        }

    monkeypatch.setattr(orchestrator_module, "get_flow_snapshot", fake_snapshot)

    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)

    session = AgentOrchestrator(capture)
    executor = _Executor()
    session._executor = executor

    await session.on_user_message("что требует внимания по документам?")

    # No dispatch to a specialist — answered by the front-agent itself.
    assert executor.user_messages == []
    # Dialogue history stays coherent.
    assert executor.external_turns and "Ожидают согласования: 3" in executor.external_turns[0][1]
    # UI contract: status + worker.assigned(secretary) + text + done.
    types = [e["type"] for e in sent]
    assert "orchestrator.status" in types
    assigned = next(e for e in sent if e["type"] == "worker.assigned")
    assert assigned["role"] == "secretary"
    text_event = next(e for e in sent if e["type"] == "text")
    assert "Ожидают согласования: 3" in text_event["content"]
    assert sent[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_secretary_falls_through_when_snapshot_unavailable(monkeypatch):
    """No snapshot → the turn goes through normal dispatch (specialist fetches data)."""
    config = BuiltinAgentConfig(
        department_enabled=True, audit_enabled=False,
        model="mock", backend_url="http://backend", ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    async def no_snapshot(cfg, **k):
        return None

    monkeypatch.setattr(orchestrator_module, "get_flow_snapshot", no_snapshot)

    async def _raise_ai(request, *a, **k):
        raise RuntimeError("llm offline")

    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)

    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)

    session = AgentOrchestrator(capture)
    executor = _Executor()
    session._executor = executor

    await session.on_user_message("что требует внимания по документам?")

    # Fall-through: the executor got the turn (normal dispatch).
    assert executor.user_messages == ["что требует внимания по документам?"]
