"""Phase 3: secretary document-flow awareness."""

from __future__ import annotations

import pytest

from app.ai import orchestrator as orchestrator_module
from app.ai import flow_awareness
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


@pytest.mark.asyncio
async def test_secretary_turn_injects_flow_context(monkeypatch):
    config = BuiltinAgentConfig(
        department_enabled=True, audit_enabled=False,
        model="mock", backend_url="http://backend", ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    async def fake_flow(cfg, **k):
        return "<flow-context>\n- Ожидают согласования (approval): 3\n</flow-context>"

    monkeypatch.setattr(orchestrator_module, "get_flow_context", fake_flow)

    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)

    role_contexts: list[str] = []

    class RecordingExecutor:
        def __init__(self, send, events):
            self._send = send
            self._events = events

        def hydrate_history(self, m):
            return None

        def recent_dialogue(self, limit=20):
            return []

        def inject_orchestrator_hint(self, h):
            return None

        def set_role_context(self, rc):
            role_contexts.append(rc or "")

        def set_response_budget(self, n):
            return None

        def set_model_override(self, model):
            return None

        async def on_approval(self, a):
            return None

        async def on_user_message(self, content):
            await self._send({"type": "text", "content": "Сводка готова."})
            await self._send({"type": "done"})

    session = AgentOrchestrator(capture)
    session._executor = RecordingExecutor(session._send_from_executor, [])

    await session.on_user_message("что требует внимания по документам?")

    assert role_contexts, "set_role_context was not called"
    rc = role_contexts[-1]
    # Both the secretary role prompt and the live flow snapshot must be present.
    assert "секретарь" in rc.lower()
    assert "approval): 3" in rc
