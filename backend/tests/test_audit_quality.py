"""Phase 4 — answer-quality audit on ALL turns (incl. text-only).

Before this, a text turn had no blocking audit check, so an empty or
hallucinated chat answer always passed.
"""

import pytest
from unittest.mock import AsyncMock

from app.ai.orchestrator import AgentOrchestrator, _looks_like_chat_table
from app.ai.audit import AuditCode
from app.ai.turn_router import TurnDecision, RecommendedTool
from app.ai.orchestrator import _decision_to_plan
from app.ai.agent_config import get_builtin_agent_config


def _orc_with_trace(text="", tools=None, workspace=False, tool_results=None):
    orc = AgentOrchestrator(send=AsyncMock())
    orc._trace.text_chunks = [text] if text else []
    orc._trace.tool_calls = list(tools or [])
    orc._trace.tool_results = list(tool_results or [])
    if workspace:
        orc._trace.workspace_events = [{"type": "workspace.updated", "canvas_id": "agent:spec-table"}]
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
        tool_results=[{"tool": "invoices", "result": {"error_code": "missing_args", "message": "x"}}],
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


def test_chat_table_detector():
    assert _looks_like_chat_table("| A | B |\n|---|---|\n| 1 | 2 |")
    assert _looks_like_chat_table("col1\tcol2\tcol3\nval1\tval2\tval3")
    assert _looks_like_chat_table("Поставщик | Сумма\nРомашка | 100")
    assert not _looks_like_chat_table("Обычный текст без таблицы.\nВторая строка.")
    assert not _looks_like_chat_table("Цена 100 | скидка 5")  # single pipe, one row
