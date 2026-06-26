"""Orchestrator recipe replay: a learned task runs without planner LLM calls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai import orchestrator as orchestrator_module
from app.ai import recipes as recipes_module
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.orchestrator import AgentOrchestrator


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

    def set_role_context(self, role_prompt) -> None:
        return None

    def set_response_budget(self, max_tokens: int) -> None:
        return None

    def set_model_override(self, model) -> None:
        return None

    def set_active_role(self, role) -> None:
        return None

    def set_excluded_tools(self, names) -> None:
        return None

    def set_workspace_expected(self, expected: bool) -> None:
        return None

    def record_external_turn(self, user_text: str, assistant_text: str) -> None:
        return None

    async def on_approval(self, approved: bool, approval_id=None, db_id=None):
        return None

    async def on_user_message(self, content: str):
        for event in self._events:
            await self._send(event)


def _recipe(status: str = "active", slots: dict | None = None,
            confirmed_replays: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        name="invoice_list__invoices_workspace",
        role="invoice_specialist",
        status=status,
        param_slots=slots,
        capability_schema_hash="",
        # Trusted by default (>= _TRUST_AFTER_CONFIRMED) so replay runs silently;
        # explainable-replay path is covered by its own test.
        confirmed_replays=confirmed_replays,
        worker_confirmations=0,
        trigger_examples=["выведи все товары в таблицу по счетам"],
        steps=[
            {"capability": "invoices", "action": "list",
             "args_template": {"action": "list"}},
            {"capability": "workspace", "action": "publish",
             "args_template": {"action": "publish"}},
        ],
    )


def _config() -> BuiltinAgentConfig:
    return BuiltinAgentConfig(
        department_enabled=True, audit_enabled=True,
        model="mock", backend_url="http://backend", ollama_url="http://ollama",
        exposed_skills=[],
    )


@pytest.mark.recipes
@pytest.mark.asyncio
async def test_active_recipe_replays_without_planning(monkeypatch):
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    recipe = _recipe()

    async def fake_find(text):
        return recipe, 0.92, 0.92

    replayed: dict = {}

    async def fake_replay(r, slots, cfg, on_event=None):
        replayed["recipe"] = r
        replayed["slots"] = slots
        if on_event:
            await on_event({
                "type": "tool_result",
                "tool": "workspace",
                "result": {"canvas_id": "agent:invoices", "message": "Таблица обновлена."},
            })
        return True

    monkeypatch.setattr(recipes_module, "find_recipe", fake_find)
    monkeypatch.setattr(recipes_module, "replay", fake_replay)

    planner_called = {"n": 0}

    async def _fail_planner(*a, **k):
        planner_called["n"] += 1
        raise AssertionError("planner LLM must not be called on recipe replay")

    monkeypatch.setattr(orchestrator_module.ai_router, "run", _fail_planner)

    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)

    session = AgentOrchestrator(capture)
    session._executor = FakeExecutor(session._send_from_executor, [])

    await session.on_user_message("выведи все товары в таблицу по счетам")

    assert replayed["recipe"] is recipe
    assert planner_called["n"] == 0
    statuses = [e for e in sent if e["type"] == "orchestrator.status"]
    assert any(e.get("plan_source") == "recipe" for e in statuses)
    text_event = next(e for e in sent if e["type"] == "text")
    assert "Таблица обновлена" in text_event["content"]
    assert sent[-1]["type"] == "done"


@pytest.mark.recipes
@pytest.mark.asyncio
async def test_failed_replay_falls_back_to_dispatch(monkeypatch):
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    async def fake_find(text):
        return _recipe(), 0.95, 0.95

    async def fake_replay(r, slots, cfg, on_event=None):
        return False

    monkeypatch.setattr(recipes_module, "find_recipe", fake_find)
    monkeypatch.setattr(recipes_module, "replay", fake_replay)

    async def _raise_ai(request, *a, **k):
        raise RuntimeError("planner offline → heuristic")

    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)

    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)

    executor_calls: list[str] = []

    class RecordingExecutor(FakeExecutor):
        async def on_user_message(self, content: str):
            executor_calls.append(content)
            await self._send({"type": "text", "content": "обычный путь"})
            await self._send({"type": "done"})

    session = AgentOrchestrator(capture)
    session._executor = RecordingExecutor(session._send_from_executor, [])

    await session.on_user_message("выведи все товары в таблицу по счетам")

    # Replay failed → normal dispatch took over.
    assert executor_calls
    assert any(
        e["type"] == "orchestrator.status" and e.get("plan_source") == "recipe_fallback"
        for e in sent
    )
    assert sent[-1]["type"] == "done"


@pytest.mark.recipes
@pytest.mark.asyncio
async def test_draft_recipe_only_hints(monkeypatch):
    """A draft recipe never replays — it becomes a worker hint instead."""
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    async def fake_find(text):
        return _recipe(status="draft"), 0.95, 0.95

    async def fail_replay(*a, **k):
        raise AssertionError("draft recipes must not replay")

    monkeypatch.setattr(recipes_module, "find_recipe", fake_find)
    monkeypatch.setattr(recipes_module, "replay", fail_replay)

    async def _raise_ai(request, *a, **k):
        raise RuntimeError("heuristic fallback")

    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)

    hints: list[str] = []

    class HintExecutor(FakeExecutor):
        def inject_orchestrator_hint(self, hint: str) -> None:
            hints.append(hint)

        async def on_user_message(self, content: str):
            await self._send({"type": "text", "content": "ок"})
            await self._send({"type": "done"})

    async def capture(msg):
        return None

    session = AgentOrchestrator(capture)
    session._executor = HintExecutor(session._send_from_executor, [])

    await session.on_user_message("выведи все товары в таблицу по счетам")

    assert hints and "invoices.list" in hints[-1]
