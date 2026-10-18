"""Turn audit on what was published: filters, claimed totals, truncated JSON.

The action check ("asked to do, only read") moved to the executor, which asks
the model itself once a turn has changed nothing (test_completion_check.py).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.ai.agent_config import get_builtin_agent_config
from app.ai.audit import AuditCode
from app.ai.orchestrator import (
    AgentOrchestrator,
    OrchestratorPlan,
    WorkerAssignment,
    WorkspaceOutputSpec,
)


def _orc(content: str, calls: list[tuple[str, dict]], text: str = "Готово") -> AgentOrchestrator:
    orc = AgentOrchestrator(send=AsyncMock())
    orc._turn_content = content
    orc._trace.text_chunks = [text]
    orc._trace.tool_calls = [tool for tool, _ in calls]
    orc._trace.tool_call_seq = list(calls)
    return orc


def _plan(intent: str = "employee"):
    return OrchestratorPlan(
        goal="задача",
        intent=intent,
        worker=WorkerAssignment(role="employee", task="задача"),
        workspace=WorkspaceOutputSpec(),
    )


# ── audit rule ───────────────────────────────────────────────────────────────


# ── Truncated-JSON salvage (extraction reliability) ─────────────────────────


def test_salvage_truncated_json_keeps_complete_rows():
    """A long extraction cut off by the token ceiling used to lose every row —
    including the hundreds that were already complete (live finding)."""
    from app.ai.ollama_client import salvage_truncated_json

    raw = (
        '{"rows": [{"part_number": "A-1", "name": "Фреза"}, '
        '{"part_number": "A-2", "name": "Сверло"}, {"part_number": "A-3", "name": "Незако'
    )
    recovered = salvage_truncated_json(raw)
    assert recovered is not None
    assert [row["part_number"] for row in recovered["rows"]] == ["A-1", "A-2"]


def test_salvage_truncated_json_returns_none_when_nothing_complete():
    from app.ai.ollama_client import salvage_truncated_json

    assert salvage_truncated_json("не json вовсе") is None
    assert salvage_truncated_json('{"rows": [{"part_number": "A-1"') is None


# ── Retry must not re-demand an action already performed ────────────────────


# ── Degraded planner must not invent requirements ───────────────────────────


# ── Filter compliance must understand spec-table filters ───────────────────


# ── A table must deliver what its title promises ────────────────────────────


def test_claimed_total_reads_counts_but_not_years():
    from app.ai.orchestrator import _claimed_total_in_title

    assert _claimed_total_in_title("Каталог поставщика — 1046 позиций") == 1046
    assert _claimed_total_in_title("Счета: всего 152 строки") == 152
    # A year is not a promise about row count.
    assert _claimed_total_in_title("Счета за 2024 год") is None
    assert _claimed_total_in_title("") is None


@pytest.mark.asyncio
async def test_audit_flags_a_table_that_shows_less_than_its_title_claims():
    """Live: 3 rows published under «… — 1046 позиций». Nothing looks broken,
    and the user believes three items are the whole catalog."""
    from app.ai.orchestrator import WorkspaceOutputSpec

    orc = _orc("Покажи каталог поставщика", [("workspace", {"action": "general"})])
    orc._trace.workspace_events = [
        {"type": "workspace.updated", "canvas_id": "agent:invoice-items"}
    ]
    orc._trace.tool_call_args = {
        "workspace": {
            "action": "general",
            "body": {"title": "Каталог поставщика «ООО Мир Станочника» — 1046 позиций"},
        }
    }
    orc._trace.tool_results = [
        {
            "tool": "workspace",
            "result": {
                "canvas_id": "agent:invoice-items",
                "status": "published",
                "total": 3,
                "shown": 3,
            },
        }
    ]
    plan = _plan(intent="analytical_table").model_copy(
        update={
            "workspace": WorkspaceOutputSpec(
                channel="workspace",
                output_type="table",
                required=True,
                canvas_id="agent:invoice-items",
            )
        }
    )
    audit = await orc._audit_turn(plan, get_builtin_agent_config())
    assert AuditCode.TOTAL_OVERSTATED.value in audit.issue_codes
    assert audit.passed is False


@pytest.mark.asyncio
async def test_audit_accepts_a_table_that_shows_what_it_claims():
    from app.ai.orchestrator import WorkspaceOutputSpec

    orc = _orc("Покажи каталог поставщика", [("workspace", {"action": "spec_table"})])
    orc._trace.workspace_events = [
        {"type": "workspace.updated", "canvas_id": "agent:invoice-items"}
    ]
    orc._trace.tool_call_args = {"workspace": {"action": "spec_table"}}
    orc._trace.tool_results = [
        {
            "tool": "workspace",
            "result": {
                "canvas_id": "agent:invoice-items",
                "status": "published",
                "total": 1046,
                "shown": 1046,
                "spec": {"title": "Каталог поставщика — 1046 позиций"},
            },
        }
    ]
    plan = _plan(intent="analytical_table").model_copy(
        update={
            "workspace": WorkspaceOutputSpec(
                channel="workspace",
                output_type="table",
                required=True,
                canvas_id="agent:invoice-items",
            )
        }
    )
    audit = await orc._audit_turn(plan, get_builtin_agent_config())
    assert AuditCode.TOTAL_OVERSTATED.value not in audit.issue_codes
