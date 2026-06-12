"""Dynamic skill runner — proxies agent-generated skills to the isolated runner.

Generated skills live in backend/app/ai/generated_skills/{skill_name}.py and
EXECUTE ONLY in the dedicated ``skill-runner`` container (non-root, read-only
fs, no credentials — see infra/skill-runner). This module never imports or
executes generated code in the backend process: it forwards the call over HTTP
and post-processes the result (cache, shadow A/B routing for the evolver).

The skill registry points generated skills to:
  POST /api/agent/generated-skill/{skill_name}
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Body, Depends, HTTPException

from app.auth.jwt import require_role
from app.auth.models import UserInfo, UserRole
from app.config import settings

logger = structlog.get_logger()

router = APIRouter()

_GENERATED_ROOT = Path(__file__).resolve().parents[1] / "ai" / "generated_skills"
_RUNNER_TIMEOUT_S = 45.0


def _safe_name(skill_name: str) -> str:
    return skill_name.replace("-", "_").replace(".", "_")


async def _run_in_runner(skill_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a generated skill in the isolated runner over HTTP."""
    url = f"{settings.skill_runner_url.rstrip('/')}/run/{_safe_name(skill_name)}"
    async with httpx.AsyncClient(timeout=_RUNNER_TIMEOUT_S) as client:
        resp = await client.post(url, json={"args": args})
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Generated skill '{skill_name}' not found")
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Skill runner error: HTTP {resp.status_code}: {resp.text[:300]}",
        )
    result = resp.json()
    return result if isinstance(result, dict) else {"status": "ok", "data": result}


@router.post("/api/agent/generated-skill/{skill_name}", tags=["agent-generated"])
async def run_generated_skill(
    skill_name: str,
    args: dict[str, Any] = Body(default_factory=dict),
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> dict[str, Any]:
    """Execute an agent-generated skill in the isolated runner."""
    # Check cache first (read-only skills only)
    try:
        from app.ai.skill_cache import get_cached
        cached = await get_cached(skill_name, args)
        if cached is not None:
            logger.debug("generated_skill_cache_hit", skill=skill_name)
            return cached
    except Exception:
        pass  # cache miss is fine

    try:
        # Shadow routing for A/B testing (skill evolver)
        from app.ai.skill_evolver import maybe_route_to_shadow, record_shadow_outcome
        shadow_name = await maybe_route_to_shadow(skill_name, args)
        if shadow_name:
            try:
                result = await _run_in_runner(shadow_name, args)
                await record_shadow_outcome(
                    skill_name, is_v2=True, success=result.get("status") == "ok"
                )
                return result
            except Exception:
                pass  # shadow failed → fall through to v1
    except Exception:
        pass

    try:
        result = await _run_in_runner(skill_name, args)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("generated_skill_runner_unreachable", skill=skill_name, error=str(exc))
        raise HTTPException(
            status_code=503,
            detail="Skill runner is unavailable; generated skills never run in-process.",
        )

    logger.info("generated_skill_executed", skill=skill_name)

    # Cache successful read results
    try:
        from app.ai.skill_cache import set_cached
        await set_cached(skill_name, args, result)
    except Exception:
        pass

    # Record outcome for skill evolver
    try:
        from app.ai.skill_evolver import record_shadow_outcome
        await record_shadow_outcome(
            skill_name, is_v2=False, success=result.get("status") == "ok"
        )
    except Exception:
        pass

    return result


@router.get("/api/agent/skill-evolution/audit", tags=["agent-generated"])
async def skill_evolution_audit(limit: int = 50) -> dict[str, Any]:
    """Return recent skill evolution audit log."""
    from app.ai.skill_evolver import get_evolution_audit
    entries = await get_evolution_audit(limit=limit)
    return {"entries": entries, "count": len(entries)}


@router.post("/api/agent/skill-evolution/evolve/{skill_name}", tags=["agent-generated"])
async def trigger_evolution(
    skill_name: str,
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> dict[str, Any]:
    """Manually trigger evolution for a specific skill."""
    from app.ai.skill_evolver import evolve_skill
    deployed = await evolve_skill(skill_name)
    return {"skill": skill_name, "shadow_deployed": deployed}


@router.get("/api/agent/skill-cache/stats", tags=["agent-generated"])
async def skill_cache_stats() -> dict[str, Any]:
    """Return skill result cache statistics."""
    from app.ai.skill_cache import get_cache_stats
    return await get_cache_stats()


def _static_skill_meta(path: Path) -> dict[str, Any]:
    """Read name/description from the file WITHOUT importing it (no code exec)."""
    info: dict[str, Any] = {"name": path.stem, "source": "agent_generated"}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception as exc:
        info["error"] = f"unparseable: {exc}"
        return info
    doc = ast.get_docstring(tree) or ""
    description = doc.strip().split("\n")[0][:120]
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "SKILL_META"
            and isinstance(node.value, ast.Dict)
        ):
            try:
                meta = ast.literal_eval(node.value)
                if isinstance(meta, dict):
                    description = str(meta.get("description") or description)[:120]
                    info["created_at"] = meta.get("created_at")
            except Exception:
                pass
    info["description"] = description
    return info


@router.get("/api/agent/generated-skills", tags=["agent-generated"])
async def list_generated_skills() -> dict[str, Any]:
    """List all available generated skills (static metadata, no import)."""
    skills = []
    if _GENERATED_ROOT.exists():
        for path in sorted(_GENERATED_ROOT.glob("*.py")):
            if path.name.startswith("_"):
                continue
            skills.append(_static_skill_meta(path))
    return {"skills": skills, "count": len(skills)}
