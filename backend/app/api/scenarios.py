"""Scenarios API — list and trigger AiAgent workflow scenarios."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.gateway_config import gateway_config
from app.ai.scenario_runner import scenario_runner
from app.db.session import get_db

router = APIRouter()


class ScenarioRunRequest(BaseModel):
    trigger: dict = {}


class ScenarioRunResponse(BaseModel):
    scenario: str
    context: dict


@router.get("")
async def list_scenarios() -> list[dict]:
    """Skill: scenarios.list — List available AiAgent scenarios."""
    return scenario_runner.list_scenarios()


@router.post("/{name}/run", response_model=ScenarioRunResponse)
async def run_scenario(name: str, body: ScenarioRunRequest) -> ScenarioRunResponse:
    """Skill: scenarios.run — Manually trigger a scenario by name."""
    if name not in gateway_config.list_scenario_names():
        raise HTTPException(status_code=404, detail=f"Scenario not found: {name!r}")
    try:
        ctx = await scenario_runner.run(name, trigger=body.trigger)
        return ScenarioRunResponse(scenario=name, context=ctx)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scenario failed: {e}")


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
