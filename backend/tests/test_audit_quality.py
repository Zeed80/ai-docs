"""Phase 4 — answer-quality audit on ALL turns (incl. text-only).

Before this, a text turn had no blocking audit check, so an empty or
hallucinated chat answer always passed.
"""

from unittest.mock import AsyncMock

import pytest

from app.ai.agent_config import get_builtin_agent_config
from app.ai.audit import AuditCode
from app.ai.orchestrator import AgentOrchestrator, _employee_plan


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


def _chat_plan():
    return _employee_plan("вопрос")


@pytest.mark.asyncio
async def test_empty_answer_blocks():
    orc = _orc_with_trace(text="", tools=[], workspace=False)
    audit = await orc._audit_turn(_chat_plan(), get_builtin_agent_config())
    assert AuditCode.EMPTY_ANSWER.value in audit.issue_codes
    assert audit.passed is False


@pytest.mark.asyncio
async def test_tool_error_recorded_advisory():
    orc = _orc_with_trace(
        text="Готово",
        tools=["invoices"],
        tool_results=[
            {"tool": "invoices", "result": {"error_code": "missing_args", "message": "x"}}
        ],
    )
    audit = await orc._audit_turn(_chat_plan(), get_builtin_agent_config())
    assert AuditCode.TOOL_ERROR.value in audit.issue_codes
    assert audit.passed is True  # advisory


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

    plan = _employee_plan("счета по поставщикам")
    orc._trace.text_chunks = ["Опубликовал таблицу «Счета по поставщикам»: 33 строк."]
    audit = await orc._audit_turn(plan, get_builtin_agent_config())
    assert AuditCode.WORKSPACE_NOT_PUBLISHED.value not in audit.issue_codes
    assert audit.workspace_verified is True
    # The client still receives the event as the tool sent it.
    assert send.await_args_list[-1].args[0]["result"]["version"] == 1
