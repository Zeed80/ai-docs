"""Phase 1 — intent-match correctness gate + adaptive-by-risk behaviour.

Hard scenarios for "агент публикует не то": an empty/mismatched desktop table
must be caught (AuditCode.INTENT_MISMATCH), retried, and — if still empty on a
cheap action — answered honestly instead of shipping a blank board. Gated
(expensive/external) actions are classified separately.
"""

from __future__ import annotations

import pytest

from app.ai import orchestrator as orchestrator_module
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.audit import RETRYABLE, AuditCode, has_code, retryable
from app.ai.orchestrator import (
    AgentOrchestrator,
    OrchestratorPlan,
    WorkerAssignment,
    WorkspaceOutputSpec,
    risk_class,
)
from app.domain.workspace import clear_workspace_blocks, upsert_workspace_block


def _config() -> BuiltinAgentConfig:
    return BuiltinAgentConfig(
        department_enabled=True, audit_enabled=True, model="mock-model",
        backend_url="http://backend", ollama_url="http://ollama", exposed_skills=[],
    )


def _plan(
    *, intent: str = "analytical_table", required: bool = True,
    skills: list[str] | None = None,
) -> OrchestratorPlan:
    return OrchestratorPlan(
        goal="выведи все фрезы и сгруппируй по поставщику",
        intent=intent,
        worker=WorkerAssignment(
            role="invoice_specialist", task="таблица",
            recommended_skills=skills or ["workspace.spec_table"],
        ),
        workspace=WorkspaceOutputSpec(
            channel="workspace" if required else "chat",
            output_type="table" if required else "text",
            required=required,
            canvas_id="agent:spec-table" if required else None,
        ),
    )


def _orchestrator(captured: list | None = None) -> AgentOrchestrator:
    async def _send(msg: dict) -> None:
        if captured is not None:
            captured.append(msg)

    return AgentOrchestrator(_send)


def _publish_event(session, *, total: int, spec: dict | None = None,
                   canvas: str = "agent:spec-table") -> None:
    """Simulate a spec-table publish landing on the desktop."""
    upsert_workspace_block(canvas, {"type": "table", "title": "T",
                                    "rows": [{"x": i} for i in range(total)]})
    session._workspace_before = {}
    session._trace.workspace_events.append({"type": "workspace.updated", "canvas_id": canvas})
    tool = "workspace__spec_table"
    session._trace.tool_calls.append(tool)
    session._trace.tool_results.append({
        "type": "tool_result", "tool": tool,
        "result": {
            "canvas_id": canvas, "status": "published", "total": total,
            "spec": spec or {"source": "invoice_items",
                             "columns": [{"field": "supplier_name", "header": "Поставщик"},
                                         {"field": "description", "header": "Наименование"}],
                             "filters": [{"field": "description", "op": "smart", "value": "фрезы резцы"}],
                             "group_by": ["supplier_name"]},
        },
    })


# ── Core: empty published table for a listing intent is flagged ────────────────

@pytest.mark.asyncio
async def test_empty_published_table_flags_intent_mismatch(monkeypatch):
    """«добавь резцы» → пустая таблица: gate ловит INTENT_MISMATCH, ход не passed."""
    clear_workspace_blocks()
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    session = _orchestrator()
    _publish_event(session, total=0)
    report = await session._audit_turn(_plan(), config)
    assert has_code(report.issues, AuditCode.INTENT_MISMATCH)
    assert report.passed is False


@pytest.mark.asyncio
async def test_non_empty_table_passes(monkeypatch):
    """8 групп фрез → НЕ ложное срабатывание."""
    clear_workspace_blocks()
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    session = _orchestrator()
    _publish_event(session, total=8)
    report = await session._audit_turn(_plan(), config)
    assert not has_code(report.issues, AuditCode.INTENT_MISMATCH)
    assert report.passed is True


@pytest.mark.asyncio
async def test_count_intent_empty_is_valid(monkeypatch):
    """count → 0 («счетов от X нет») — валидный ответ, НЕ mismatch."""
    clear_workspace_blocks()
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    session = _orchestrator()
    _publish_event(session, total=0)
    report = await session._audit_turn(_plan(intent="count"), config)
    assert not has_code(report.issues, AuditCode.INTENT_MISMATCH)


@pytest.mark.asyncio
async def test_self_correction_last_publish_wins(monkeypatch):
    """Hardest: первый publish пустой, второй — исправленный (8 строк). Берём
    ПОСЛЕДНИЙ → mismatch не выставляется (ход сам себя починил)."""
    clear_workspace_blocks()
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    session = _orchestrator()
    _publish_event(session, total=0)
    _publish_event(session, total=8)  # corrected republish — last wins
    report = await session._audit_turn(_plan(), config)
    assert not has_code(report.issues, AuditCode.INTENT_MISMATCH)
    assert report.passed is True


@pytest.mark.asyncio
async def test_non_spectable_publish_ignored(monkeypatch):
    """Опубликован блок без spec (другой инструмент) → intent-match не трогает."""
    clear_workspace_blocks()
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    session = _orchestrator()
    upsert_workspace_block("agent:spec-table", {"type": "note", "rows": []})
    session._workspace_before = {}
    session._trace.workspace_events.append(
        {"type": "workspace.updated", "canvas_id": "agent:spec-table"})
    session._trace.tool_calls.append("workspace__pin_note")
    session._trace.tool_results.append({
        "type": "tool_result", "tool": "workspace__pin_note",
        "result": {"canvas_id": "agent:spec-table", "status": "published"},  # no 'spec'
    })
    report = await session._audit_turn(_plan(), config)
    assert not has_code(report.issues, AuditCode.INTENT_MISMATCH)


# ── Risk classification ────────────────────────────────────────────────────────

def test_risk_class_cheap_for_spec_table():
    assert risk_class(_plan(skills=["workspace.spec_table"])) == "cheap"


def test_risk_class_gated_for_external_actions():
    assert risk_class(_plan(skills=["email.send"])) == "gated"
    assert risk_class(_plan(skills=["invoice.approve"])) == "gated"
    assert risk_class(_plan(skills=["anomaly.resolve", "workspace.spec_table"])) == "gated"


def test_intent_mismatch_is_retryable():
    from app.ai.audit import AuditIssue
    assert AuditCode.INTENT_MISMATCH in RETRYABLE
    assert retryable([AuditIssue(code=AuditCode.INTENT_MISMATCH, message="x")]) is True


# ── Critic snapshot + adaptive honest message ──────────────────────────────────

@pytest.mark.asyncio
async def test_published_table_brief_for_critic(monkeypatch):
    clear_workspace_blocks()
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    session = _orchestrator()
    _publish_event(session, total=8)
    brief = session._published_table_brief()
    assert "invoice_items" in brief
    assert "группировка" in brief and "supplier_name" in brief
    assert "строк=8" in brief


@pytest.mark.asyncio
async def test_adaptive_honest_message_on_empty(monkeypatch):
    """Cheap turn, пустая таблица → честное сообщение, а не молчаливая пустышка."""
    clear_workspace_blocks()
    config = _config()
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)
    captured: list = []
    session = _orchestrator(captured)
    _publish_event(session, total=0)
    await session._explain_intent_mismatch(_plan(), "выведи все фрезы")
    texts = [m["content"] for m in captured if m.get("type") == "text"]
    assert texts and "ничего не нашлось" in texts[0]
    assert "invoice_items" in texts[0]
