"""Scenarios API — list and trigger AiAgent workflow scenarios."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.ai.gateway_config import gateway_config
from app.ai.scenario_runner import scenario_runner

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
