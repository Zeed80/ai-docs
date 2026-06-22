"""TurnDecision dispatch integration (Phase 3).

Verifies the orchestrator routes each intent to the right deterministic executor
(flow-status / spec-patch) or to the worker, by *intent* — not substring.
The executors themselves are mocked; the live routing quality is covered by the
raw-model trap test.
"""

import pytest
from unittest.mock import AsyncMock

from app.ai.orchestrator import AgentOrchestrator, _decision_to_plan
from app.ai.turn_router import TurnDecision, RecommendedTool


def _orc() -> AgentOrchestrator:
    return AgentOrchestrator(send=AsyncMock())


def test_decision_to_plan_workspace_analytical():
    d = TurnDecision(
        intent="analytical_table",
        role="data_analyst",
        output_channel="workspace",
        recommended=[RecommendedTool(capability="invoices", action="list")],
        goal="счета за май",
    )
    plan = _decision_to_plan(d, "счета за май")
    assert plan.workspace.required is True
    assert plan.workspace.channel == "workspace"
    # Content-aware canvas: an invoice request lands on the specialised invoice
    # surface, not the generic spec-table funnel.
    assert plan.workspace.canvas_id == "agent:invoices"
    assert plan.worker.role == "data_analyst"
    assert plan.worker.recommended_skills == ["invoices.list"]


def test_decision_to_plan_unmatched_workspace_falls_back_to_spec_table():
    """A workspace turn with no specialised route still gets the universal
    SQL-backed spec-table surface (never an empty canvas)."""
    d = TurnDecision(
        intent="analytical_table", role="data_analyst", output_channel="workspace",
        goal="сводка по непонятной сущности кверти",
    )
    plan = _decision_to_plan(d, "сводка по непонятной сущности кверти")
    assert plan.workspace.required is True
    assert plan.workspace.canvas_id == "agent:spec-table"


def test_decision_to_plan_chat_specialist():
    d = TurnDecision(intent="specialist", output_channel="chat", goal="вопрос")
    plan = _decision_to_plan(d, "вопрос")
    assert plan.workspace.required is False
    assert plan.workspace.canvas_id is None
    assert plan.workspace.output_type == "text"


@pytest.mark.asyncio
async def test_dispatch_flow_status_uses_secretary(monkeypatch):
    orc = _orc()
    flow = AsyncMock(return_value=True)
    patch_tbl = AsyncMock(return_value=False)
    run = AsyncMock()
    monkeypatch.setattr(orc, "_answer_flow_status_directly", flow)
    monkeypatch.setattr(orc, "_try_spec_table_patch_directly", patch_tbl)
    monkeypatch.setattr(orc, "_run_planned_turn", run)

    d = TurnDecision(intent="flow_status", output_channel="chat")
    await orc._dispatch_decision("что в работе", d, _cfg(), 0.0, "normal")

    flow.assert_awaited_once()
    run.assert_not_awaited()  # secretary handled it, no worker run


@pytest.mark.asyncio
async def test_dispatch_table_edit_uses_patch(monkeypatch):
    orc = _orc()
    patch_tbl = AsyncMock(return_value=True)
    run = AsyncMock()
    monkeypatch.setattr(orc, "_try_spec_table_patch_directly", patch_tbl)
    monkeypatch.setattr(orc, "_run_planned_turn", run)

    d = TurnDecision(intent="table_edit", output_channel="workspace")
    await orc._dispatch_decision("добавь столбец НДС", d, _cfg(), 0.0, "normal")

    patch_tbl.assert_awaited_once()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_analytical_runs_worker(monkeypatch):
    orc = _orc()
    run = AsyncMock()
    recipe = AsyncMock(return_value="")  # no recipe
    monkeypatch.setattr(orc, "_run_planned_turn", run)
    monkeypatch.setattr(orc, "_try_recipe_for_turn", recipe)

    d = TurnDecision(intent="analytical_table", output_channel="workspace")
    await orc._dispatch_decision("покажи счета", d, _cfg(), 0.0, "normal")

    run.assert_awaited_once()
    plan = run.await_args.args[1]
    assert plan.workspace.required is True


@pytest.mark.asyncio
async def test_dispatch_table_edit_falls_through_when_not_a_patch(monkeypatch):
    orc = _orc()
    patch_tbl = AsyncMock(return_value=False)  # not actually a patch
    run = AsyncMock()
    recipe = AsyncMock(return_value="")
    monkeypatch.setattr(orc, "_try_spec_table_patch_directly", patch_tbl)
    monkeypatch.setattr(orc, "_run_planned_turn", run)
    monkeypatch.setattr(orc, "_try_recipe_for_turn", recipe)

    d = TurnDecision(intent="table_edit", output_channel="workspace")
    await orc._dispatch_decision("и отправь поставщику", d, _cfg(), 0.0, "normal")

    patch_tbl.assert_awaited_once()
    run.assert_awaited_once()  # fell through to worker


def _cfg():
    from app.ai.agent_config import get_builtin_agent_config
    return get_builtin_agent_config()
