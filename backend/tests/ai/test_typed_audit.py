"""Typed audit issues: codes drive retry/repair/gap control flow, not message text."""

from __future__ import annotations

import pytest

from app.ai import orchestrator as orchestrator_module
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.audit import (
    AuditCode,
    AuditIssue,
    blocking,
    has_code,
)
from app.ai.model_tier import Tier, aux_quality_budget
from app.ai.orchestrator import (
    AgentOrchestrator,
    AuditReport,
    OrchestratorPlan,
    WorkerAssignment,
    WorkspaceOutputSpec,
)


def _issue(code: AuditCode, severity: str = "blocking") -> AuditIssue:
    return AuditIssue(code=code, severity=severity, message="любой текст — не для матчинга")


def _plan(*, workspace_required: bool = True, filters: dict | None = None) -> OrchestratorPlan:
    return OrchestratorPlan(
        goal="тест",
        intent="invoice_list",
        worker=WorkerAssignment(
            role="invoice_specialist",
            task="тест",
            recommended_skills=["workspace.invoice_items_table"],
        ),
        workspace=WorkspaceOutputSpec(
            channel="workspace" if workspace_required else "chat",
            output_type="table" if workspace_required else "text",
            required=workspace_required,
            canvas_id="agent:invoice-items" if workspace_required else None,
            filters=filters or {},
        ),
    )


def _orchestrator() -> AgentOrchestrator:
    async def _noop(_msg: dict) -> None:
        return None

    return AgentOrchestrator(_noop)


# ── audit.py primitives ────────────────────────────────────────────────────────


def test_blocking_excludes_advisory():
    issues = [
        _issue(AuditCode.TOOL_ERROR, severity="advisory"),
        _issue(AuditCode.EMPTY_ANSWER),
    ]
    assert [i.code for i in blocking(issues)] == [AuditCode.EMPTY_ANSWER]
    assert has_code(issues, AuditCode.TOOL_ERROR)


# ── Orchestrator control flow on codes ─────────────────────────────────────────


# ── Aux LLM budget ─────────────────────────────────────────────────────────────


def test_aux_quality_budget_by_tier():
    assert aux_quality_budget(Tier.NANO) == 1
    assert aux_quality_budget(Tier.MEDIUM) == 1
    assert aux_quality_budget(Tier.LARGE) == 2
    assert aux_quality_budget(Tier.EXPERT) == 2


def test_aux_quality_budget_reads_routes_yml_not_hardcoded(monkeypatch):
    """A4 regression guard: changing routes.yml's aux_quality_budgets must
    change the returned value with zero code edits — this was a hardcoded
    ``2 if tier >= Tier.LARGE else 1`` in model_tier.py before A4."""
    from app.ai import route_table

    monkeypatch.setattr(route_table, "aux_quality_budgets", lambda: {"large": 5, "default": 3})
    assert aux_quality_budget(Tier.NANO) == 3
    assert aux_quality_budget(Tier.LARGE) == 5


def test_response_budget_reads_routes_yml_not_hardcoded(monkeypatch):
    """A4 regression guard: same for orchestrator._response_budget_for's
    response_budgets — was hardcoded 8192/4096/1024/2048 before A4."""
    from app.ai import orchestrator as orchestrator_module
    from app.ai import route_table

    monkeypatch.setattr(
        route_table,
        "response_budgets",
        lambda: {
            "by_output_type": {"table": 9999},
            "by_tier": {"large": 7777, "medium": 5555, "micro": 3333},
            "default": 1111,
        },
    )
    worker = orchestrator_module.WorkerAssignment(role="data_analyst", task="t")
    plan = orchestrator_module.OrchestratorPlan(
        goal="g",
        intent="test",
        worker=worker,
        workspace=orchestrator_module.WorkspaceOutputSpec(output_type="table"),
    )
    assert orchestrator_module._response_budget_for(Tier.NANO, plan) == 9999  # output_type wins

    plan_chat = orchestrator_module.OrchestratorPlan(
        goal="g",
        intent="test",
        worker=worker,
        workspace=orchestrator_module.WorkspaceOutputSpec(output_type="text"),
    )
    assert orchestrator_module._response_budget_for(Tier.LARGE, plan_chat) == 7777
    assert orchestrator_module._response_budget_for(Tier.MEDIUM, plan_chat) == 5555
    assert orchestrator_module._response_budget_for(Tier.MICRO, plan_chat) == 3333
    assert orchestrator_module._response_budget_for(Tier.SMALL, plan_chat) == 1111


@pytest.mark.asyncio
async def test_semantic_audit_respects_budget(monkeypatch):
    """When the aux budget is exhausted, no further LLM audit calls are made."""
    config = BuiltinAgentConfig(
        audit_enabled=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    calls = 0

    async def _count_run(request, *args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("must not be called when budget is exhausted")

    monkeypatch.setattr(orchestrator_module.ai_router, "run", _count_run)

    session = _orchestrator()
    session._tier = Tier.EXPERT
    session._aux_llm_calls = aux_quality_budget(Tier.EXPERT)  # exhausted
    session._trace.text_chunks.append("какой-то ответ")
    report = AuditReport(passed=True, issues=[])

    await session._run_semantic_audit(_plan(workspace_required=False), config, report)
    assert calls == 0
    assert report.semantic_passed is None  # no verdict, not a fake success


@pytest.mark.asyncio
async def test_semantic_audit_infra_failure_leaves_verdict_unknown(monkeypatch):
    config = BuiltinAgentConfig(
        audit_enabled=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    async def _boom(request, *args, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(orchestrator_module.ai_router, "run", _boom)

    session = _orchestrator()
    session._tier = Tier.EXPERT
    session._trace.text_chunks.append("какой-то ответ")
    report = AuditReport(passed=True, issues=[])

    await session._run_semantic_audit(_plan(workspace_required=False), config, report)
    assert report.semantic_passed is None
