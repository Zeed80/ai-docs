from __future__ import annotations

import pytest

from app.ai import orchestrator as orchestrator_module
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.orchestrator import AgentOrchestrator
from app.domain.workspace import clear_workspace_blocks, upsert_workspace_block


class FakeExecutor:
    def __init__(self, send, events):
        self._send = send
        self._events = events

    def hydrate_history(self, messages):
        return None

    async def on_approval(self, approved: bool):
        return None

    async def on_user_message(self, content: str):
        for event in self._events:
            if callable(event):
                event()
                continue
            await self._send(event)


@pytest.mark.asyncio
async def test_orchestrator_assigns_worker_and_audits_workspace(monkeypatch):
    clear_workspace_blocks()
    upsert_workspace_block(
        "agent:invoice-items-grouped",
        {"type": "table", "title": "Товары", "rows": [{"id": 1}]},
    )
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)
    sent: list[dict] = []

    async def capture(message: dict):
        sent.append(message)

    session = AgentOrchestrator(capture)
    session._executor = FakeExecutor(
        session._send_from_executor,
        [
            lambda: upsert_workspace_block(
                "agent:invoice-items-grouped",
                {"type": "table", "title": "Товары", "rows": [{"id": 1}, {"id": 2}]},
            ),
            {
                "type": "tool_result",
                "tool": "workspace__invoice_items_grouped_table",
                "result": {"canvas_id": "agent:invoice-items-grouped"},
            },
            {"type": "text", "content": "Открыл таблицу на Рабочем столе."},
            {"type": "done"},
        ],
    )

    await session.on_user_message("Выведи все товары в таблицу, сгруппируй по счетам")

    assert [event["type"] for event in sent[:3]] == [
        "orchestrator.status",
        "worker.assigned",
        "workspace.publish_started",
    ]
    assert any(event["type"] == "workspace.publish_verified" for event in sent)
    assert any(event["type"] == "audit.passed" for event in sent)
    assert sent[-1] == {"type": "done"}
    assert sum(1 for event in sent if event["type"] == "done") == 1


@pytest.mark.asyncio
async def test_orchestrator_reports_capability_gap_when_workspace_missing(monkeypatch):
    clear_workspace_blocks()
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=True,
        allow_capability_builder=True,
        capability_builder_requires_approval=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)
    sent: list[dict] = []

    async def capture(message: dict):
        sent.append(message)

    session = AgentOrchestrator(capture)
    session._executor = FakeExecutor(
        session._send_from_executor,
        [{"type": "text", "content": "Готово"}, {"type": "done"}],
    )

    await session.on_user_message("Выведи полный список документов в таблицу")

    assert any(event["type"] == "audit.failed" for event in sent)
    assert any(event["type"] == "capability_gap.detected" for event in sent)
    assert any(event["type"] == "capability_gap.builder_draft" for event in sent)
    assert sent[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_orchestrator_rejects_stale_workspace_block(monkeypatch):
    clear_workspace_blocks()
    upsert_workspace_block(
        "agent:invoice-items-grouped",
        {"type": "table", "title": "Старый блок", "rows": [{"id": 1}]},
    )
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=True,
        allow_capability_builder=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)
    sent: list[dict] = []

    async def capture(message: dict):
        sent.append(message)

    session = AgentOrchestrator(capture)
    session._executor = FakeExecutor(
        session._send_from_executor,
        [
            {
                "type": "tool_result",
                "tool": "workspace__invoice_items_grouped_table",
                "result": {"canvas_id": "agent:invoice-items-grouped"},
            },
            {"type": "text", "content": "Готово"},
            {"type": "done"},
        ],
    )

    await session.on_user_message("Выведи все товары в таблицу, сгруппируй по счетам")

    assert any(event["type"] == "audit.failed" for event in sent)
    assert not any(event["type"] == "workspace.publish_verified" for event in sent)


def test_orchestrator_treats_column_edit_as_workspace_request(monkeypatch):
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)

    async def capture(message: dict):
        return None

    session = AgentOrchestrator(capture)
    plan = session._plan_turn("Добавь столбец с названием поставщика перед номером счета")

    assert plan.worker.role == "invoice_specialist"
    assert plan.workspace.required is True
    assert plan.workspace.output_type == "table"
    assert plan.workspace.canvas_id == "agent:invoice-items-grouped"


def test_orchestrator_targets_latest_open_table_for_vague_followup(monkeypatch):
    clear_workspace_blocks()
    upsert_workspace_block(
        "agent:invoice-items-grouped",
        {"type": "table", "title": "Товары", "rows": [{"id": 1}]},
    )
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)

    async def capture(message: dict):
        return None

    session = AgentOrchestrator(capture)
    plan = session._plan_turn("Добавь еще информацию по поставщикам в уже открытую таблицу")

    assert plan.workspace.required is True
    assert plan.workspace.canvas_id == "agent:invoice-items-grouped"


@pytest.mark.asyncio
async def test_orchestrator_retries_when_executor_uses_wrong_workspace_tool(monkeypatch):
    clear_workspace_blocks()
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=True,
        allow_capability_builder=True,
        max_audit_retries=1,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)
    sent: list[dict] = []
    calls = 0

    class RetryExecutor(FakeExecutor):
        async def on_user_message(self, content: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                await self._send({
                    "type": "tool_call",
                    "tool": "workspace__invoice_items_grouped_table",
                    "args": {},
                })
                await self._send({
                    "type": "tool_result",
                    "tool": "workspace__invoice_items_grouped_table",
                    "result": {"canvas_id": "agent:invoice-items-grouped"},
                })
                return
            upsert_workspace_block(
                "agent:invoice-items-by-supplier",
                {"type": "table", "title": "По поставщикам", "rows": [{"id": 1}]},
            )
            await self._send({
                "type": "tool_call",
                "tool": "workspace__invoice_items_by_supplier_table",
                "args": {},
            })
            await self._send({
                "type": "tool_result",
                "tool": "workspace__invoice_items_by_supplier_table",
                "result": {"canvas_id": "agent:invoice-items-by-supplier"},
            })

    async def capture(message: dict):
        sent.append(message)

    session = AgentOrchestrator(capture)
    session._executor = RetryExecutor(session._send_from_executor, [])

    await session.on_user_message("Выведи товары в таблицу, сгруппируй по поставщикам")

    assert calls == 2
    assert any(event["type"] == "audit.retry_started" for event in sent)
    assert any(event["type"] == "audit.passed" for event in sent)
    assert not any(event["type"] == "capability_gap.detected" for event in sent)


@pytest.mark.asyncio
async def test_orchestrator_directly_executes_expected_tool_when_retry_disabled(monkeypatch):
    clear_workspace_blocks()
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=True,
        allow_capability_builder=True,
        max_audit_retries=0,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(orchestrator_module.ai_router, "run", _raise_ai)
    posted: list[tuple[str, dict]] = []
    sent: list[dict] = []

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {
                "status": "published",
                "canvas_id": "agent:invoice-items-by-supplier",
                "total": 1,
                "shown": 1,
                "message": "Открыл таблицу по поставщикам.",
            }

    class FakeHttpClient:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url: str, json: dict):
            posted.append((url, json))
            upsert_workspace_block(
                "agent:invoice-items-by-supplier",
                {"type": "table", "title": "По поставщикам", "rows": [{"id": 1}]},
            )
            return Response()

    monkeypatch.setattr(orchestrator_module.httpx, "AsyncClient", FakeHttpClient)

    async def capture(message: dict):
        sent.append(message)

    session = AgentOrchestrator(capture)
    session._executor = FakeExecutor(
        session._send_from_executor,
        [
            {
                "type": "tool_call",
                "tool": "workspace__invoice_table",
                "args": {},
            },
            {
                "type": "tool_result",
                "tool": "workspace__invoice_table",
                "result": {"canvas_id": "agent:invoice-list"},
            },
        ],
    )

    await session.on_user_message("Выведи товары в таблицу, сгруппируй по поставщикам")

    assert posted
    assert posted[0][0] == "http://backend/api/workspace/agent/invoices/items-by-supplier-table"
    assert posted[0][1]["canvas_id"] == "agent:invoice-items-by-supplier"
    assert any(event["type"] == "orchestrator.direct_tool_started" for event in sent)
    assert any(event["type"] == "audit.passed" for event in sent)
    assert not any(event["type"] == "capability_gap.detected" for event in sent)


@pytest.mark.asyncio
async def test_orchestrator_uses_model_plan(monkeypatch):
    clear_workspace_blocks()
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=False,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    model_plan = orchestrator_module.OrchestratorPlan(
        goal="Показать договоры",
        intent="procurement_contracts",
        worker=orchestrator_module.WorkerAssignment(
            role="procurement_specialist",
            task="Показать договоры",
            recommended_skills=["procurement.list_contracts"],
        ),
        workspace=orchestrator_module.WorkspaceOutputSpec(
            channel="workspace",
            output_type="table",
            required=True,
            canvas_id="agent:contracts",
        ),
    )

    class Response:
        data = model_plan

    async def fake_run(request):
        return Response()

    async def capture(message: dict):
        return None

    monkeypatch.setattr(orchestrator_module.ai_router, "run", fake_run)
    session = AgentOrchestrator(capture)
    plan = await session._plan_turn_with_model("Покажи договоры поставщиков таблицей", config)

    assert plan.worker.role == "procurement_specialist"
    assert plan.workspace.canvas_id == "agent:contracts"


@pytest.mark.asyncio
async def test_orchestrator_prompt_includes_dialog_history(monkeypatch):
    clear_workspace_blocks()
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=False,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    captured_prompt = ""

    class Response:
        data = orchestrator_module.OrchestratorPlan(
            goal="Обновить таблицу",
            intent="invoice_data",
            worker=orchestrator_module.WorkerAssignment(
                role="invoice_specialist",
                task="Обновить таблицу",
                recommended_skills=["workspace.invoice_items_by_supplier_table"],
            ),
            workspace=orchestrator_module.WorkspaceOutputSpec(
                channel="workspace",
                output_type="table",
                required=True,
                canvas_id="agent:invoice-items-by-supplier",
            ),
        )

    async def fake_run(request):
        nonlocal captured_prompt
        captured_prompt = request.messages[-1].content
        return Response()

    async def capture(message: dict):
        return None

    monkeypatch.setattr(orchestrator_module.ai_router, "run", fake_run)
    session = AgentOrchestrator(capture)
    session.hydrate_history([
        {"role": "user", "content": "Выведи товары по счетам"},
        {"role": "assistant", "content": "Открыл таблицу."},
    ])
    await session._plan_turn_with_model("Теперь сгруппируй по поставщикам", config)

    assert "Выведи товары по счетам" in captured_prompt
    assert "Теперь сгруппируй по поставщикам" in captured_prompt


async def _raise_ai(*args, **kwargs):
    raise RuntimeError("AI unavailable in test")
