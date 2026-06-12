"""Typed audit issues: codes drive retry/repair/gap control flow, not message text."""

from __future__ import annotations

import pytest

from app.ai import orchestrator as orchestrator_module
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.audit import (
    RETRYABLE,
    AuditCode,
    AuditIssue,
    blocking,
    has_code,
    retryable,
)
from app.ai.model_tier import Tier, aux_quality_budget
from app.ai.orchestrator import (
    AgentOrchestrator,
    AuditReport,
    OrchestratorPlan,
    WorkerAssignment,
    WorkspaceOutputSpec,
)
from app.domain.workspace import clear_workspace_blocks, upsert_workspace_block


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


def test_retryable_is_code_driven_not_text_driven():
    # The message deliberately contains none of the old Russian markers.
    issues = [_issue(AuditCode.FILTER_MISSING)]
    assert retryable(issues) is True

    # A non-retryable code with marker-like text must NOT trigger retry.
    weird = AuditIssue(
        code=AuditCode.UNKNOWN_SKILL,
        message="фильтр не применён, публикация не подтверждена",  # red herring
    )
    assert retryable([weird]) is False


def test_blocking_excludes_advisory():
    issues = [
        _issue(AuditCode.TOOL_OFF_PLAN, severity="advisory"),
        _issue(AuditCode.WRONG_CANVAS),
    ]
    assert [i.code for i in blocking(issues)] == [AuditCode.WRONG_CANVAS]
    assert has_code(issues, AuditCode.TOOL_OFF_PLAN)


def test_retryable_set_contents():
    assert AuditCode.UNKNOWN_SKILL not in RETRYABLE
    assert AuditCode.TOOL_OFF_PLAN not in RETRYABLE
    for code in (
        AuditCode.WORKSPACE_NOT_PUBLISHED,
        AuditCode.WRONG_CANVAS,
        AuditCode.CHAT_TABLE_LEAK,
        AuditCode.FILTER_MISSING,
        AuditCode.FILTER_MISMATCH,
    ):
        assert code in RETRYABLE


# ── Orchestrator control flow on codes ─────────────────────────────────────────


def test_can_retry_with_executor_uses_codes():
    session = _orchestrator()
    plan = _plan()

    report = AuditReport(passed=False, issues=[_issue(AuditCode.WRONG_CANVAS)])
    assert session._can_retry_with_executor(plan, report) is True

    # UNKNOWN_SKILL blocks the retry even when paired with a retryable issue.
    report = AuditReport(
        passed=False,
        issues=[_issue(AuditCode.WRONG_CANVAS), _issue(AuditCode.UNKNOWN_SKILL)],
    )
    assert session._can_retry_with_executor(plan, report) is False

    # Advisory-only report → nothing to fix by retrying.
    report = AuditReport(
        passed=True, issues=[_issue(AuditCode.TOOL_OFF_PLAN, severity="advisory")]
    )
    assert session._can_retry_with_executor(plan, report) is False


@pytest.mark.asyncio
async def test_tool_off_plan_is_advisory_and_does_not_fail_turn(monkeypatch):
    """An equivalent-but-unplanned tool that publishes correctly passes the audit."""
    clear_workspace_blocks()
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    session = _orchestrator()
    plan = _plan()
    # Workspace was actually published and verified via another workspace tool.
    upsert_workspace_block(
        "agent:invoice-items", {"type": "table", "title": "Счета", "rows": [{"id": 1}]}
    )
    session._workspace_before = {}
    session._trace.workspace_events.append(
        {"type": "workspace.updated", "canvas_id": "agent:invoice-items"}
    )
    session._trace.tool_calls.append("workspace__another_equivalent_table")

    report = await session._audit_turn(plan, config)
    assert has_code(report.issues, AuditCode.TOOL_OFF_PLAN)
    assert report.passed is True  # advisory must not flip `passed`


@pytest.mark.asyncio
async def test_filter_audit_checks_last_publish_not_first(monkeypatch):
    """A turn that publishes wrong first and corrects itself must pass."""
    clear_workspace_blocks()
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    session = _orchestrator()
    plan = _plan(filters={"supplier_query": "ООО Ромашка"})
    upsert_workspace_block(
        "agent:invoice-items", {"type": "table", "title": "Счета", "rows": [{"id": 1}]}
    )
    session._workspace_before = {}
    session._trace.workspace_events.append(
        {"type": "workspace.updated", "canvas_id": "agent:invoice-items"}
    )
    tool = "workspace__invoice_items_table"
    session._trace.tool_calls.append(tool)
    # First publish: filter forgotten. Second publish: corrected.
    session._trace.tool_results.append(
        {"type": "tool_result", "tool": tool, "result": {"canvas_id": "agent:invoice-items"}}
    )
    session._trace.tool_call_args[tool] = {"supplier_query": "ООО Ромашка"}
    session._trace.tool_results.append(
        {
            "type": "tool_result",
            "tool": tool,
            "result": {
                "canvas_id": "agent:invoice-items",
                "filters": {"supplier_query": "ООО Ромашка"},
            },
        }
    )

    report = await session._audit_turn(plan, config)
    assert not has_code(report.issues, AuditCode.FILTER_MISSING, AuditCode.FILTER_MISMATCH)
    assert report.passed is True


@pytest.mark.asyncio
async def test_filter_mismatch_in_final_publish_is_flagged(monkeypatch):
    clear_workspace_blocks()
    config = BuiltinAgentConfig(
        department_enabled=True,
        audit_enabled=True,
        model="mock-model",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    session = _orchestrator()
    plan = _plan(filters={"supplier_query": "ООО Ромашка"})
    upsert_workspace_block(
        "agent:invoice-items", {"type": "table", "title": "Счета", "rows": [{"id": 1}]}
    )
    session._workspace_before = {}
    session._trace.workspace_events.append(
        {"type": "workspace.updated", "canvas_id": "agent:invoice-items"}
    )
    tool = "workspace__invoice_items_table"
    session._trace.tool_calls.append(tool)
    session._trace.tool_call_args[tool] = {"supplier_query": "ООО Лютик"}
    session._trace.tool_results.append(
        {"type": "tool_result", "tool": tool, "result": {"canvas_id": "agent:invoice-items"}}
    )

    report = await session._audit_turn(plan, config)
    assert has_code(report.issues, AuditCode.FILTER_MISMATCH)
    assert report.passed is False


# ── Aux LLM budget ─────────────────────────────────────────────────────────────


def test_aux_quality_budget_by_tier():
    assert aux_quality_budget(Tier.NANO) == 1
    assert aux_quality_budget(Tier.MEDIUM) == 1
    assert aux_quality_budget(Tier.LARGE) == 2
    assert aux_quality_budget(Tier.EXPERT) == 2


@pytest.mark.asyncio
async def test_semantic_audit_respects_budget(monkeypatch):
    """When the aux budget is exhausted, no further LLM audit calls are made."""
    config = BuiltinAgentConfig(
        department_enabled=True,
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
        department_enabled=True,
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
