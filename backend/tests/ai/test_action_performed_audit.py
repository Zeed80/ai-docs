"""An action turn must actually perform the action, not publish a look-alike.

Live finding (2026-08-21 chat session «Поиск каталогов для Мир Станочника»):
"найди каталоги поставщика и загрузи в его каталог" ended with a summary table
on the desktop and a note in memory — nothing was attached to the supplier, and
the turn was reported as done. The audit below is what turns that into a
blocking issue (retry, then an honest message with one clarifying question).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.ai.agent_config import get_builtin_agent_config
from app.ai.audit import AuditCode
from app.ai.orchestrator import (
    AgentOrchestrator,
    _decision_to_plan,
    _is_state_changing_call,
)
from app.ai.turn_router import TurnDecision


def _orc(content: str, calls: list[tuple[str, dict]], text: str = "Готово") -> AgentOrchestrator:
    orc = AgentOrchestrator(send=AsyncMock())
    orc._turn_content = content
    orc._trace.text_chunks = [text]
    orc._trace.tool_calls = [tool for tool, _ in calls]
    orc._trace.tool_call_seq = list(calls)
    return orc


def _plan(intent: str = "specialist"):
    return _decision_to_plan(TurnDecision(intent=intent, output_channel="chat"), "задача")


# ── _is_state_changing_call ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool,args,expected",
    [
        ("tool_catalog", {"action": "attach_web_catalog"}, True),
        ("tool_catalog", {"action": "resolve_supplier"}, False),
        ("tool_catalog", {"action": "list_entries"}, False),
        # Finding catalogs is not attaching them.
        ("tool_catalog", {"action": "discover_catalogs"}, False),
        ("email", {"action": "send"}, True),
        ("suppliers", {"action": "get"}, False),
        # Reporting capabilities never count, whatever the action.
        ("workspace", {"action": "spec_table"}, False),
        ("workspace__invoice_items_table", {}, False),
        ("memory", {"action": "source_propose"}, False),
        ("search", {"action": "browse"}, False),
    ],
)
def test_state_change_classification(tool, args, expected):
    assert _is_state_changing_call(tool, args) is expected


# ── audit rule ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_action_turn_with_only_reads_is_blocking():
    orc = _orc(
        "Найди в web каталоги на поставщика ООО Мир Станочника и загрузи в его каталог",
        [
            ("search", {"action": "browse", "url": "https://example.test/catalog/"}),
            ("memory", {"action": "source_propose", "url": "https://example.test/catalog/"}),
            ("workspace", {"action": "general"}),
        ],
    )
    audit = await orc._audit_turn(_plan(), get_builtin_agent_config())
    assert AuditCode.ACTION_NOT_PERFORMED.value in audit.issue_codes
    assert audit.passed is False


@pytest.mark.asyncio
async def test_action_turn_that_performed_the_action_passes():
    orc = _orc(
        "Загрузи каталог с сайта и прикрепи к поставщику ООО Мир Станочника",
        [
            ("search", {"action": "browse", "url": "https://example.test/catalog.pdf"}),
            ("tool_catalog", {"action": "attach_web_catalog", "supplier_name": "ООО Мир Станочника"}),
        ],
    )
    audit = await orc._audit_turn(_plan(), get_builtin_agent_config())
    assert AuditCode.ACTION_NOT_PERFORMED.value not in audit.issue_codes


@pytest.mark.asyncio
async def test_table_turn_is_not_treated_as_an_unperformed_action():
    """"обнови таблицу" is an action verb whose action IS the publication."""
    orc = _orc(
        "обнови таблицу и отсортируй по сумме",
        [("workspace", {"action": "spec_table_patch"})],
    )
    audit = await orc._audit_turn(_plan(intent="table_edit"), get_builtin_agent_config())
    assert AuditCode.ACTION_NOT_PERFORMED.value not in audit.issue_codes


@pytest.mark.asyncio
async def test_plain_question_never_flags_the_action_rule():
    orc = _orc("сколько счетов от Мир Станочника?", [("invoices", {"action": "list"})])
    audit = await orc._audit_turn(_plan(intent="answer_self"), get_builtin_agent_config())
    assert AuditCode.ACTION_NOT_PERFORMED.value not in audit.issue_codes


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


@pytest.mark.asyncio
async def test_action_performed_in_an_earlier_pass_is_remembered():
    """The trace is reset between passes; the fact that the action happened is
    not. Without this the second audit failed a turn that had already attached
    the catalogs, and the repair path overwrote the agent's report (live)."""
    orc = _orc(
        "Найди все каталоги поставщика и прикрепи их",
        [("tool_catalog", {"action": "attach_web_catalog"})],
    )
    first = await orc._audit_turn(_plan(), get_builtin_agent_config())
    assert AuditCode.ACTION_NOT_PERFORMED.value not in first.issue_codes

    # Retry: fresh trace, only a read this time.
    orc._trace.tool_calls = ["workspace"]
    orc._trace.tool_call_seq = [("workspace", {"action": "general"})]
    second = await orc._audit_turn(_plan(), get_builtin_agent_config())
    assert AuditCode.ACTION_NOT_PERFORMED.value not in second.issue_codes


def test_listing_phrasing_does_not_make_an_action_turn_a_table_turn():
    """«Найди ВСЕ каталоги … и прикрепи» trips the listing markers, but the
    desktop table it implied is what overwrote the real answer."""
    from unittest.mock import AsyncMock as _AsyncMock

    from app.ai.orchestrator import AgentOrchestrator as _Orc

    orc = _Orc(send=_AsyncMock())
    action_plan = orc._plan_turn(
        "Найди в интернете все каталоги поставщика ООО Мир Станочника и прикрепи их к этому поставщику"
    )
    assert action_plan.workspace.required is False

    # A genuine listing request is unaffected.
    listing_plan = orc._plan_turn("Выведи все счета за май")
    assert listing_plan.workspace.required is True


# ── Degraded planner must not invent requirements ───────────────────────────


def test_heuristic_plan_invents_no_filters_for_an_action_turn():
    """The invented supplier_query filter was what failed the live turn with
    "фильтр не применён" and triggered a pointless retry."""
    from unittest.mock import AsyncMock as _AsyncMock

    from app.ai.orchestrator import AgentOrchestrator as _Orc, _normalize_model_plan

    orc = _Orc(send=_AsyncMock())
    content = "Найди все каталоги поставщика ООО Мир Станочника и прикрепи их к нему"
    plan = _normalize_model_plan(orc._plan_turn(content), content)

    assert plan.workspace.required is False
    assert plan.workspace.filters == {}
    assert plan.worker.role == "procurement_specialist"
    assert any(s.startswith("tool_catalog.") for s in plan.worker.recommended_skills)


def test_heuristic_plan_still_routes_a_real_table_request():
    from unittest.mock import AsyncMock as _AsyncMock

    from app.ai.orchestrator import AgentOrchestrator as _Orc, _normalize_model_plan

    orc = _Orc(send=_AsyncMock())
    content = "Покажи счета поставщика ООО Мир Станочника"
    plan = _normalize_model_plan(orc._plan_turn(content), content)

    assert plan.workspace.required is True
    assert plan.workspace.canvas_id
    assert plan.workspace.filters.get("supplier_query")


# ── Filter compliance must understand spec-table filters ───────────────────


def test_spec_filter_on_real_column_satisfies_plan_filter():
    """The plan asks for supplier_query=X; a spec table expresses that as
    supplier_name contains X. Treating those as different cost a live turn a
    19-minute retry for a filter that WAS applied."""
    from app.ai.orchestrator import _filter_satisfied_by_spec

    spec = {
        "source": "invoices",
        "filters": [{"field": "supplier_name", "op": "contains", "value": "Мир Станочника"}],
    }
    assert _filter_satisfied_by_spec("supplier_query", "ооо мир станочника", spec) is True
    assert _filter_satisfied_by_spec("supplier_query", "Мир Станочника", spec) is True
    # A different supplier is still a miss.
    assert _filter_satisfied_by_spec("supplier_query", "ЦНК", spec) is False
    # An unfiltered table is a miss too.
    assert _filter_satisfied_by_spec("supplier_query", "Мир Станочника", {"filters": []}) is False


@pytest.mark.asyncio
async def test_audit_accepts_spec_filtered_table():
    from app.ai.orchestrator import WorkspaceOutputSpec

    orc = _orc("Покажи счета поставщика ООО Мир Станочника", [("workspace", {"action": "spec_table"})])
    orc._trace.workspace_events = [
        {"type": "workspace.updated", "canvas_id": "agent:invoices"}
    ]
    orc._trace.tool_call_args = {"workspace": {"action": "spec_table"}}
    orc._trace.tool_results = [{
        "tool": "workspace",
        "result": {
            "canvas_id": "agent:invoices",
            "status": "published",
            "total": 3,
            "filters": {},
            "spec": {
                "source": "invoices",
                "filters": [
                    {"field": "supplier_name", "op": "contains", "value": "Мир Станочника"}
                ],
            },
        },
    }]
    plan = _plan(intent="analytical_table")
    plan = plan.model_copy(update={
        "workspace": WorkspaceOutputSpec(
            channel="workspace", output_type="table", required=True,
            canvas_id="agent:invoices", filters={"supplier_query": "ооо мир станочника"},
        )
    })
    audit = await orc._audit_turn(plan, get_builtin_agent_config())
    assert AuditCode.FILTER_MISSING.value not in audit.issue_codes
