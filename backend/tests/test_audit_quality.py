"""Phase 4 — answer-quality audit on ALL turns (incl. text-only).

Before this, a text turn had no blocking audit check, so an empty or
hallucinated chat answer always passed.
"""

from unittest.mock import AsyncMock

import pytest

from app.ai.agent_config import get_builtin_agent_config
from app.ai.audit import AuditCode
from app.ai.orchestrator import AgentOrchestrator, _decision_to_plan, _looks_like_chat_table
from app.ai.turn_router import RecommendedTool, TurnDecision


def _orc_with_trace(text="", tools=None, workspace=False, tool_results=None):
    orc = AgentOrchestrator(send=AsyncMock())
    orc._trace.text_chunks = [text] if text else []
    orc._trace.tool_calls = list(tools or [])
    orc._trace.tool_results = list(tool_results or [])
    if workspace:
        orc._trace.workspace_events = [
            {"type": "workspace.updated", "canvas_id": "agent:spec-table"}
        ]
    return orc


def _chat_plan(intent="specialist"):
    return _decision_to_plan(TurnDecision(intent=intent, output_channel="chat"), "вопрос")


@pytest.mark.asyncio
async def test_empty_answer_blocks():
    orc = _orc_with_trace(text="", tools=[], workspace=False)
    audit = await orc._audit_turn(_chat_plan(), get_builtin_agent_config())
    assert AuditCode.EMPTY_ANSWER.value in audit.issue_codes
    assert audit.passed is False


@pytest.mark.asyncio
async def test_ungrounded_factual_is_advisory_not_blocking():
    long = "Себестоимость фрезеровки складывается из множества факторов и расчётов." * 2
    orc = _orc_with_trace(text=long, tools=[], workspace=False)
    audit = await orc._audit_turn(_chat_plan(intent="answer_self"), get_builtin_agent_config())
    assert AuditCode.UNGROUNDED_ANSWER.value in audit.issue_codes
    assert audit.passed is True  # advisory does not flip passed


@pytest.mark.asyncio
async def test_grounded_factual_no_ungrounded_flag():
    orc = _orc_with_trace(text="У вас 7 счетов на проверке.", tools=["invoices"], workspace=False)
    audit = await orc._audit_turn(_chat_plan(intent="answer_self"), get_builtin_agent_config())
    assert AuditCode.UNGROUNDED_ANSWER.value not in audit.issue_codes


@pytest.mark.asyncio
async def test_tool_error_recorded_advisory():
    orc = _orc_with_trace(
        text="Готово",
        tools=["invoices"],
        tool_results=[
            {"tool": "invoices", "result": {"error_code": "missing_args", "message": "x"}}
        ],
    )
    audit = await orc._audit_turn(_chat_plan(intent="answer_self"), get_builtin_agent_config())
    assert AuditCode.TOOL_ERROR.value in audit.issue_codes
    assert audit.passed is True  # advisory


@pytest.mark.asyncio
async def test_tool_off_plan_advisory_when_recommended_capability_unused():
    # Recommended invoices, but the worker used documents instead → advisory only.
    orc = _orc_with_trace(text="Готово", tools=["documents"])
    plan = _decision_to_plan(
        TurnDecision(
            intent="answer_self",
            output_channel="chat",
            recommended=[RecommendedTool(capability="invoices", action="list")],
        ),
        "счета",
    )
    audit = await orc._audit_turn(plan, get_builtin_agent_config())
    assert AuditCode.TOOL_OFF_PLAN.value in audit.issue_codes
    assert audit.passed is True  # advisory never blocks


@pytest.mark.asyncio
async def test_no_tool_off_plan_when_recommended_capability_used():
    orc = _orc_with_trace(text="Готово", tools=["invoices"])
    plan = _decision_to_plan(
        TurnDecision(
            intent="answer_self",
            output_channel="chat",
            recommended=[RecommendedTool(capability="invoices", action="list")],
        ),
        "счета",
    )
    audit = await orc._audit_turn(plan, get_builtin_agent_config())
    assert AuditCode.TOOL_OFF_PLAN.value not in audit.issue_codes


@pytest.mark.asyncio
async def test_workspace_required_satisfied_by_not_found_tool_result():
    """See AGENT_LIVE_TEST_FINDINGS.md #5: workspace.invoice_items_table /
    workspace.spec_table return status="not_found" instead of publishing
    when the named supplier doesn't exist. That must count as a completed
    turn, not a WORKSPACE_NOT_PUBLISHED gap that triggers a capability-gap
    proposal for a tool the agent already has and used correctly."""
    orc = _orc_with_trace(
        text="Поставщик «ЦНК» не найден в базе.",
        tools=["workspace"],
        tool_results=[
            {
                "tool": "workspace",
                "result": {"status": "not_found", "canvas_id": "agent:invoice-items"},
            }
        ],
    )
    plan = _decision_to_plan(
        TurnDecision(intent="analytical_table", output_channel="workspace"),
        "счета от ЦНК",
    )
    audit = await orc._audit_turn(plan, get_builtin_agent_config())
    assert AuditCode.WORKSPACE_NOT_PUBLISHED.value not in audit.issue_codes
    assert audit.workspace_verified is True


@pytest.mark.asyncio
async def test_workspace_required_still_flagged_without_not_found_result():
    """A genuine miss (worker never called the workspace tool at all) must
    still be flagged — the not_found exception is narrow, not a blanket
    pass for any unpublished workspace-required turn."""
    orc = _orc_with_trace(text="Готово", tools=[])
    plan = _decision_to_plan(
        TurnDecision(intent="analytical_table", output_channel="workspace"),
        "счета",
    )
    audit = await orc._audit_turn(plan, get_builtin_agent_config())
    assert AuditCode.WORKSPACE_NOT_PUBLISHED.value in audit.issue_codes
    assert audit.workspace_verified is False


def test_chat_table_detector():
    assert _looks_like_chat_table("| A | B |\n|---|---|\n| 1 | 2 |")
    assert _looks_like_chat_table("col1\tcol2\tcol3\nval1\tval2\tval3")
    assert _looks_like_chat_table("Поставщик | Сумма\nРомашка | 100")
    assert not _looks_like_chat_table("Обычный текст без таблицы.\nВторая строка.")
    assert not _looks_like_chat_table("Цена 100 | скидка 5")  # single pipe, one row


@pytest.mark.asyncio
async def test_a_sql_table_published_in_a_v1_envelope_counts_as_published(monkeypatch):
    """The audit read the tool's status at the top level of the ToolResult v1
    envelope: a published SQL table registered no workspace event, the turn
    was audited "not published" and retried (live 2026-10-07)."""
    from app.ai import orchestrator as orch_module

    send = AsyncMock()
    orc = AgentOrchestrator(send=send)
    orc._workspace_before = {}
    monkeypatch.setattr(
        orch_module, "get_workspace_block", lambda cid: {"id": cid, "updated_at": "now"}
    )
    event = {
        "type": "tool_result",
        "tool": "workspace",
        "result": {
            "version": 1,
            "status": "succeeded",
            "data": {"status": "published", "canvas_id": "agent:spec-table", "total": 33},
        },
    }

    await orc._send_from_executor(event)

    plan = _decision_to_plan(
        TurnDecision(intent="analytical_table", output_channel="workspace"),
        "счета по поставщикам",
    )
    orc._trace.text_chunks = ["Опубликовал таблицу «Счета по поставщикам»: 33 строк."]
    audit = await orc._audit_turn(plan, get_builtin_agent_config())
    assert AuditCode.WORKSPACE_NOT_PUBLISHED.value not in audit.issue_codes
    assert audit.workspace_verified is True
    # The client still receives the event as the tool sent it.
    assert send.await_args_list[-1].args[0]["result"]["version"] == 1


@pytest.mark.asyncio
async def test_a_not_found_result_in_a_v1_envelope_is_still_not_found():
    orc = AgentOrchestrator(send=AsyncMock())
    await orc._send_from_executor(
        {
            "type": "tool_result",
            "tool": "workspace",
            "result": {"version": 1, "status": "succeeded", "data": {"status": "not_found"}},
        }
    )
    assert orc._has_not_found_tool_result() is True
