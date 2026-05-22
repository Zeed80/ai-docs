"""Scenarios API — list and trigger AiAgent workflow scenarios."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.gateway_config import gateway_config
from app.ai.scenario_runner import scenario_runner
from app.db.session import get_db

router = APIRouter()

# Tools that create approval gates when requested
_APPROVAL_GATE_TOOLS = {
    "email.send.request_approval",
    "invoice.approve",
    "tech.process_plan_from_drawing",
    "table.apply_diff",
    "anomaly.resolve",
    "warehouse.confirm_receipt",
    "bom.approve",
}


class ScenarioRunRequest(BaseModel):
    trigger: dict = {}
    case_id: uuid.UUID | None = None
    draft_id: str | None = None
    requested_tools: list[str] = []


class ScenarioRunResponse(BaseModel):
    scenario: str
    context: dict
    approval_gates: list[dict] = []


@router.get("")
async def list_scenarios() -> list[dict]:
    """Skill: scenarios.list — List available AiAgent scenarios."""
    return scenario_runner.list_scenarios()


@router.post("/{name}/run", response_model=ScenarioRunResponse)
async def run_scenario(
    name: str, body: ScenarioRunRequest, db: AsyncSession = Depends(get_db)
) -> ScenarioRunResponse:
    """Skill: scenarios.run — Manually trigger a scenario by name."""
    if name not in gateway_config.list_scenario_names():
        raise HTTPException(status_code=404, detail=f"Scenario not found: {name!r}")

    trigger = dict(body.trigger)
    if body.case_id:
        trigger["case_id"] = str(body.case_id)
    if body.draft_id:
        trigger["draft_id"] = body.draft_id

    try:
        ctx = await scenario_runner.run(name, trigger=trigger)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scenario failed: {e}")

    # Create approval gates for any requested approval tools linked to the case
    gates: list[dict] = []
    if body.case_id:
        gate_tools = set(body.requested_tools) & _APPROVAL_GATE_TOOLS
        if gate_tools:
            from app.db.models import Approval, ApprovalActionType, ApprovalStatus, AuditTimelineEvent

            for tool_name in gate_tools:
                # Map tool name to enum value (fallback: email_send)
                try:
                    action_enum = ApprovalActionType(tool_name)
                except ValueError:
                    action_enum = ApprovalActionType.email_send
                approval = Approval(
                    entity_type="case",
                    entity_id=body.case_id,
                    action_type=action_enum,
                    status=ApprovalStatus.pending,
                    requested_by="agent",
                    context={"scenario": name, "draft_id": body.draft_id, "tool": tool_name},
                )
                db.add(approval)
                await db.flush()

                db.add(AuditTimelineEvent(
                    entity_type="case",
                    entity_id=body.case_id,
                    event_type="approval_gate_created",
                    actor="agent",
                    summary=f"Требуется подтверждение: {tool_name}",
                    details={"approval_id": str(approval.id), "tool": tool_name, "scenario": name},
                ))

                gates.append({
                    "id": str(approval.id),
                    "action_type": tool_name,
                    "status": "pending",
                    "requested_by": "agent",
                    "context": approval.context,
                    "created_at": approval.created_at.isoformat() if approval.created_at else None,
                })

            await db.commit()

    return ScenarioRunResponse(scenario=name, context=ctx, approval_gates=gates)


@router.get("/traces")
async def list_traces(
    scenario_name: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List recent scenario execution traces (newest first)."""
    from app.db.models import ScenarioTrace

    stmt = select(ScenarioTrace).order_by(ScenarioTrace.started_at.desc()).limit(limit)
    if scenario_name:
        stmt = stmt.where(ScenarioTrace.scenario_name == scenario_name)
    result = await db.execute(stmt)
    traces = result.scalars().all()
    return [
        {
            "id": str(t.id),
            "scenario_name": t.scenario_name,
            "status": t.status,
            "steps_total": t.steps_total,
            "steps_done": t.steps_done,
            "duration_ms": t.duration_ms,
            "error": t.error,
            "started_at": t.started_at.isoformat(),
            "finished_at": t.finished_at.isoformat() if t.finished_at else None,
            "triggered_by": t.triggered_by,
            "step_traces": t.step_traces,
        }
        for t in traces
    ]


@router.post("/agent/reload-config", status_code=200)
async def reload_agent_config() -> dict:
    """Hot-reload gateway.yml without server restart.

    After calling this endpoint, the next chat session will use
    the updated skill whitelist, approval gates, and model config.
    """
    gateway_config.reload()
    return {
        "status": "reloaded",
        "exposed_skills": len(gateway_config.exposed_skills),
        "approval_gates": sorted(gateway_config.approval_gates),
        "model": gateway_config.reasoning_model,
        "scenarios": gateway_config.list_scenario_names(),
    }
