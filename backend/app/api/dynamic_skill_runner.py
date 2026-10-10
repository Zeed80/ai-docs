"""Retired generated-skill surface (E48).

Agent-generated skills, their evolution (shadow A/B promotion) and the result
cache behind them are gone: nothing generated code could be promoted any more
(capability ``sandbox-apply``/``decide``/``promote`` answer 410), so the
builder, evolver, sandbox and the ``skill-runner`` container only produced
files and beat runs nobody could use. Work that needs code runs as a
task-scoped ``script`` step of a work order (E26–E30).

Old clients get an explicit 410 with that explanation, never a silent empty
answer or a fallback to another executor.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()

RETIRED_DETAIL = (
    "Generated skills, skill evolution and the skill cache are retired (E48); "
    "code runs as a task-scoped script step of a work order"
)


def _retired() -> None:
    raise HTTPException(status_code=410, detail=RETIRED_DETAIL)


@router.post(
    "/api/agent/generated-skill/{skill_name}", tags=["agent-generated"], include_in_schema=False
)
async def run_generated_skill(skill_name: str) -> None:
    _retired()


@router.get("/api/agent/generated-skills", tags=["agent-generated"], include_in_schema=False)
async def list_generated_skills() -> None:
    _retired()


@router.get("/api/agent/skill-evolution/audit", tags=["agent-generated"], include_in_schema=False)
async def skill_evolution_audit() -> None:
    _retired()


@router.post(
    "/api/agent/skill-evolution/evolve/{skill_name}",
    tags=["agent-generated"],
    include_in_schema=False,
)
async def trigger_evolution(skill_name: str) -> None:
    _retired()


@router.get("/api/agent/skill-cache/stats", tags=["agent-generated"], include_in_schema=False)
async def skill_cache_stats() -> None:
    _retired()
