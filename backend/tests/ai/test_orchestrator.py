from __future__ import annotations

import pytest

from app.ai import orchestrator as orchestrator_module
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.orchestrator import AgentOrchestrator
from app.domain.workspace import clear_workspace_blocks


class FakeExecutor:
    def __init__(self, send, events):
        self._send = send
        self._events = events

    def hydrate_history(self, messages):
        return None

    def recent_dialogue(self, limit: int = 20):
        return []

    def inject_orchestrator_hint(self, hint: str) -> None:
        return None

    def set_response_budget(self, max_tokens: int) -> None:
        return None

    def set_model_override(self, model) -> None:
        return None

    def record_external_turn(self, user_text: str, assistant_text: str) -> None:
        return None

    async def request_confirmation(self, *args, **kwargs):
        return True

    async def on_approval(self, approved: bool, approval_id=None, db_id=None):
        return None

    async def on_user_message(self, content: str):
        for event in self._events:
            if callable(event):
                event()
                continue
            await self._send(event)


async def _raise_ai(*args, **kwargs):
    raise RuntimeError("AI unavailable in test")


@pytest.mark.asyncio
async def test_semantic_audit_emits_soft_warning(monkeypatch):
    """The semantic audit surfaces a soft quality warning without failing the turn."""
    from types import SimpleNamespace

    clear_workspace_blocks()
    config = BuiltinAgentConfig(
        audit_enabled=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    async def _fake_run(request, *args, **kwargs):
        # Planning path: data is not an OrchestratorPlan → heuristic fallback.
        # Semantic-audit path: parsed from .text.
        return SimpleNamespace(
            data=None,
            text='{"ok": false, "reason": "не приведены конкретные цифры"}',
        )

    monkeypatch.setattr(orchestrator_module.ai_router, "run", _fake_run)
    sent: list[dict] = []

    async def capture(message: dict):
        sent.append(message)

    session = AgentOrchestrator(capture)
    session._executor = FakeExecutor(
        session._send_from_executor,
        [{"type": "text", "content": "Цены выросли."}, {"type": "done"}],
    )

    # High-complexity → Tier>=MEDIUM → semantic audit runs.
    await session.on_user_message("Проанализируй подробно динамику цен поставщиков")

    semantic = next((e for e in sent if e["type"] == "audit.semantic"), None)
    assert semantic is not None
    assert semantic["semantic_passed"] is False
    assert "цифр" in semantic["semantic_reason"]
    # Soft signal must not turn the turn itself into a failure.
    assert sent[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_reactive_refine_revises_flagged_generative_answer(monkeypatch):
    """A failed semantic audit on a text answer triggers one revise pass."""
    from types import SimpleNamespace

    from app.ai.schemas import AITask

    clear_workspace_blocks()
    config = BuiltinAgentConfig(
        audit_enabled=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    async def _fake_run(request, *args, **kwargs):
        if request.task == AITask.CLASSIFICATION:
            return SimpleNamespace(data=None, text='{"ok": false, "reason": "нет конкретных цифр"}')
        if request.task == AITask.EMAIL_DRAFTING:
            return SimpleNamespace(data=None, text="Цены выросли на 12% за квартал.")
        # planning path → not an OrchestratorPlan → heuristic fallback
        return SimpleNamespace(data=None, text="")

    monkeypatch.setattr(orchestrator_module.ai_router, "run", _fake_run)
    sent: list[dict] = []

    async def capture(message: dict):
        sent.append(message)

    session = AgentOrchestrator(capture)
    session._executor = FakeExecutor(
        session._send_from_executor,
        [{"type": "text", "content": "Цены изменились."}, {"type": "done"}],
    )

    await session.on_user_message("Проанализируй подробно динамику цен поставщиков")

    revised = next((e for e in sent if e["type"] == "answer.revised"), None)
    assert revised is not None
    assert "12%" in revised["content"]
    assert sent[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_tier_based_model_routing(monkeypatch):
    """Simple turns route to fast_model; complex turns use the default model."""
    config = BuiltinAgentConfig(
        audit_enabled=False,
        model="qwen3.6:35b",
        fast_model="qwen3.5:9b",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)

    overrides: list = []

    class ModelRecordingExecutor(FakeExecutor):
        def set_model_override(self, model) -> None:
            overrides.append(model)

    async def capture(msg):
        pass

    # Simple turn ("сколько счетов" → low tier) → fast_model.
    session = AgentOrchestrator(capture)
    session._executor = ModelRecordingExecutor(
        session._send_from_executor, [{"type": "text", "content": "ok"}, {"type": "done"}]
    )
    await session.on_user_message("сколько счетов")
    assert overrides[-1] == "qwen3.5:9b"

    # Complex turn (high-complexity signal) → default model (None override).
    overrides.clear()
    session2 = AgentOrchestrator(capture)
    session2._executor = ModelRecordingExecutor(
        session2._send_from_executor, [{"type": "text", "content": "ok"}, {"type": "done"}]
    )
    await session2.on_user_message("проанализируй и сравни подробно динамику цен поставщиков")
    assert overrides[-1] is None


@pytest.mark.asyncio
async def test_a_turn_goes_to_the_employee_without_a_routing_call(monkeypatch):
    """One employee: no router call before the executor, no role, no plan hint."""
    config = BuiltinAgentConfig(
        audit_enabled=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)

    hints: list[str] = []
    received: list[str] = []

    class RecordingExecutor(FakeExecutor):
        def inject_orchestrator_hint(self, hint: str) -> None:
            hints.append(hint)

        async def on_user_message(self, content: str):
            received.append(content)
            await super().on_user_message(content)

    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)

    session = AgentOrchestrator(capture)
    session._executor = RecordingExecutor(
        session._send_from_executor,
        [{"type": "text", "content": "Напоминание создано."}, {"type": "done"}],
    )
    await session.on_user_message("Напомни в пятницу позвонить в Эвал-Никель")

    assert received == ["Напомни в пятницу позвонить в Эвал-Никель"]
    assert hints == []
    assert not any(e["type"] in ("orchestrator.status", "worker.assigned") for e in sent)
    assert sent[-1]["type"] == "done"
