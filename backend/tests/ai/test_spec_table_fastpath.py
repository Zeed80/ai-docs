"""Orchestrator fast-path: recognised table edits apply with 0 LLM calls."""

from __future__ import annotations

import pytest

from app.ai import orchestrator as orchestrator_module
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.orchestrator import AgentOrchestrator
from app.domain.workspace import clear_workspace_blocks, upsert_workspace_block


class FakeExecutor:
    def __init__(self, send):
        self._send = send
        self.user_messages: list[str] = []
        self.external_turns: list[tuple[str, str]] = []

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

    def set_model_override(self, m):
        return None

    def set_active_role(self, r):
        return None

    def set_excluded_tools(self, tools):
        return None

    def set_workspace_expected(self, expected):
        return None

    def record_external_turn(self, u, a):
        self.external_turns.append((u, a))

    async def on_approval(self, a, approval_id=None, db_id=None):
        return None

    async def on_user_message(self, content):
        self.user_messages.append(content)
        await self._send({"type": "text", "content": "обычный путь"})
        await self._send({"type": "done"})


def _spec_block() -> dict:
    return {
        "id": "agent:spec-table",
        "type": "table",
        "title": "Счета",
        "columns": [],
        "rows": [],
        "spec": {
            "source": "invoices",
            "columns": [
                {"field": "supplier_name"},
                {"field": "invoice_number"},
                {"field": "total_amount"},
            ],
            "filters": [],
            "sort": [],
        },
        "source": "workspace.spec_table",
    }


def _config() -> BuiltinAgentConfig:
    cfg = BuiltinAgentConfig(
        department_enabled=True, audit_enabled=True,
        model="mock", backend_url="http://backend", ollama_url="http://ollama",
        exposed_skills=[],
    )
    cfg.use_turn_router = False
    return cfg


def _legacy_config() -> BuiltinAgentConfig:
    return _config()


@pytest.mark.asyncio
async def test_table_edit_applies_without_llm(monkeypatch):
    clear_workspace_blocks()
    upsert_workspace_block("agent:spec-table", _spec_block())
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    async def _no_llm(request, *a, **k):
        raise AssertionError("LLM must not be called for a recognised table edit")

    monkeypatch.setattr(orchestrator_module.ai_router, "run", _no_llm)

    posted: list[tuple[str, dict]] = []

    class FakeResponse:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {
                "status": "published",
                "canvas_id": "agent:spec-table",
                "total": 3,
                "shown": 3,
                "message": "добавил столбец «НДС». Таблица «Счета»: 3 строк — полные данные из БД.",
            }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):  # noqa: A002
            posted.append((url, json or {}))
            return FakeResponse()

    monkeypatch.setattr(orchestrator_module.httpx, "AsyncClient", FakeClient)

    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)

    session = AgentOrchestrator(capture)
    executor = FakeExecutor(session._send_from_executor)
    session._executor = executor

    await session.on_user_message("добавь столбец с ндс перед суммой")

    # Patch endpoint hit with deterministic ops, executor (LLM) never ran.
    assert executor.user_messages == []
    url, payload = posted[0]
    assert url.endswith("/api/workspace/agent/spec-table/patch")
    assert payload["ops"][0]["op"] == "add_column"
    assert payload["ops"][0]["field"] == "tax_amount"
    assert payload["ops"][0]["before"] == "total_amount"
    # User got the result and history stays coherent.
    text_event = next(e for e in sent if e["type"] == "text")
    assert "НДС" in text_event["content"]
    assert executor.external_turns
    assert sent[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_active_sheet_edit_uses_sheet_endpoint(monkeypatch):
    clear_workspace_blocks()
    upsert_workspace_block("sheet:11111111-1111-1111-1111-111111111111", {
        "id": "sheet:11111111-1111-1111-1111-111111111111",
        "type": "sheet",
        "title": "Лист",
        "sheet_id": "11111111-1111-1111-1111-111111111111",
        "columns": [
            {"key": "A", "header": "A", "type": "text"},
            {"key": "B", "header": "B", "type": "text"},
        ],
        "rows": [{"A": "x", "B": "y"}],
        "raw_rows": [{"A": "x", "B": "y"}],
        "layout": {"merges": []},
    })
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", _legacy_config)

    posted: list[tuple[str, dict]] = []

    class FakeResponse:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"status": "merged"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):  # noqa: A002
            posted.append((url, json or {}))
            return FakeResponse()

    monkeypatch.setattr(orchestrator_module.httpx, "AsyncClient", FakeClient)
    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)

    session = AgentOrchestrator(capture)
    executor = FakeExecutor(session._send_from_executor)
    session._executor = executor

    await session.on_user_message(
        "объедини A1:B1",
        workspace_context={
            "active_tabular_surface": {
                "id": "sheet:11111111-1111-1111-1111-111111111111",
                "kind": "sheet",
                "sheet_id": "11111111-1111-1111-1111-111111111111",
                "write_policy": "scratch",
            }
        },
    )

    assert executor.user_messages == []
    url, payload = posted[0]
    assert url.endswith("/api/workspace/sheets/11111111-1111-1111-1111-111111111111/merge-cells")
    assert payload == {"start_row": 0, "end_row": 0, "start_col": "A", "end_col": "B"}
    assert any(e["type"] == "text" and "Объединил" in e["content"] for e in sent)


@pytest.mark.asyncio
async def test_no_spec_table_falls_through(monkeypatch):
    """Without a spec table the edit goes through normal dispatch."""
    clear_workspace_blocks()
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    async def _raise(request, *a, **k):
        raise RuntimeError("heuristic fallback")

    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise)

    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)

    session = AgentOrchestrator(capture)
    executor = FakeExecutor(session._send_from_executor)
    session._executor = executor

    await session.on_user_message("добавь столбец с ндс перед суммой")
    assert executor.user_messages, "no spec table → normal dispatch must run"


@pytest.mark.asyncio
async def test_failed_patch_falls_through(monkeypatch):
    """Patch endpoint error → normal dispatch takes over (no dead end)."""
    clear_workspace_blocks()
    upsert_workspace_block("agent:spec-table", _spec_block())
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    async def _raise(request, *a, **k):
        raise RuntimeError("heuristic fallback")

    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise)

    class FakeResponse:
        status_code = 500
        content = b"{}"

        @staticmethod
        def json():
            return {"status": "error", "message": "db down"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):  # noqa: A002
            return FakeResponse()

    monkeypatch.setattr(orchestrator_module.httpx, "AsyncClient", FakeClient)

    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)

    session = AgentOrchestrator(capture)
    executor = FakeExecutor(session._send_from_executor)
    session._executor = executor

    await session.on_user_message("добавь столбец с ндс перед суммой")
    assert executor.user_messages, "failed patch → executor fallback"
