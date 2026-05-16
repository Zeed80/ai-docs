"""Dynamic skill runner — executes agent-generated skills without uvicorn restart.

Generated skills live in backend/app/ai/generated_skills/{skill_name}.py.
Each module must expose: async def execute(args: dict) -> dict

The skill registry points generated skills to:
  POST /api/agent/generated-skill/{skill_name}
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Body, HTTPException

logger = structlog.get_logger()

router = APIRouter()

_GENERATED_ROOT = Path(__file__).resolve().parents[1] / "ai" / "generated_skills"


def _load_skill_module(skill_name: str):
    """Import (or reload) a generated skill module by name."""
    safe = skill_name.replace("-", "_").replace(".", "_")
    module_name = f"app.ai.generated_skills.{safe}"
    module_path = _GENERATED_ROOT / f"{safe}.py"

    if not module_path.exists():
        raise FileNotFoundError(f"Generated skill file not found: {module_path}")

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec for {module_path}")

    if module_name in sys.modules:
        module = sys.modules[module_name]
        importlib.reload(module)
    else:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

    return module


@router.post("/api/agent/generated-skill/{skill_name}", tags=["agent-generated"])
async def run_generated_skill(
    skill_name: str,
    args: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Execute an agent-generated skill module."""
    try:
        module = _load_skill_module(skill_name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Generated skill '{skill_name}' not found")
    except Exception as exc:
        logger.error("generated_skill_load_failed", skill=skill_name, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to load skill: {exc}")

    if not hasattr(module, "execute"):
        raise HTTPException(
            status_code=500,
            detail=f"Skill module '{skill_name}' has no execute() function",
        )

    # Check cache first (read-only skills only)
    try:
        from app.ai.skill_cache import get_cached, set_cached
        cached = await get_cached(skill_name, args)
        if cached is not None:
            logger.debug("generated_skill_cache_hit", skill=skill_name)
            return cached
    except Exception:
        pass  # cache miss is fine

    is_v2 = False
    try:
        # Check shadow routing for A/B testing
        from app.ai.skill_evolver import maybe_route_to_shadow, record_shadow_outcome
        shadow_name = await maybe_route_to_shadow(skill_name, args)
        if shadow_name:
            try:
                shadow_module = _load_skill_module(shadow_name)
                if hasattr(shadow_module, "execute"):
                    result = await shadow_module.execute(args)
                    await record_shadow_outcome(skill_name, is_v2=True, success=result.get("status") == "ok")
                    return result
            except Exception:
                pass  # shadow failed → fall through to v1
    except Exception:
        pass

    try:
        result = await module.execute(args)
        logger.info("generated_skill_executed", skill=skill_name)

        # Cache successful read results
        try:
            await set_cached(skill_name, args, result)
        except Exception:
            pass

        # Record outcome for skill evolver
        try:
            from app.ai.skill_evolver import record_shadow_outcome
            await record_shadow_outcome(skill_name, is_v2=False, success=result.get("status") == "ok")
        except Exception:
            pass

        return result
    except NotImplementedError:
        logger.warning("generated_skill_not_implemented", skill=skill_name)
        return {"status": "error", "skill": skill_name, "message": "Skill is a stub and not yet implemented"}
    except Exception as exc:
        import traceback
        logger.error(
            "generated_skill_execution_failed",
            skill=skill_name,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        return {"status": "error", "skill": skill_name, "message": str(exc)}


@router.get("/api/agent/skill-evolution/audit", tags=["agent-generated"])
async def skill_evolution_audit(limit: int = 50) -> dict[str, Any]:
    """Return recent skill evolution audit log."""
    from app.ai.skill_evolver import get_evolution_audit
    entries = await get_evolution_audit(limit=limit)
    return {"entries": entries, "count": len(entries)}


@router.post("/api/agent/skill-evolution/evolve/{skill_name}", tags=["agent-generated"])
async def trigger_evolution(skill_name: str) -> dict[str, Any]:
    """Manually trigger evolution for a specific skill."""
    from app.ai.skill_evolver import evolve_skill
    deployed = await evolve_skill(skill_name)
    return {"skill": skill_name, "shadow_deployed": deployed}


@router.get("/api/agent/skill-cache/stats", tags=["agent-generated"])
async def skill_cache_stats() -> dict[str, Any]:
    """Return skill result cache statistics."""
    from app.ai.skill_cache import get_cache_stats
    return await get_cache_stats()


@router.get("/api/agent/generated-skills", tags=["agent-generated"])
async def list_generated_skills() -> dict[str, Any]:
    """List all available generated skills."""
    skills = []
    if _GENERATED_ROOT.exists():
        for path in sorted(_GENERATED_ROOT.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                module = _load_skill_module(path.stem)
                doc = getattr(module, "__doc__", "") or ""
                meta = getattr(module, "SKILL_META", {})
                skills.append({
                    "name": path.stem,
                    "description": meta.get("description") or doc.strip().split("\n")[0][:120],
                    "created_at": meta.get("created_at"),
                    "source": "agent_generated",
                })
            except Exception as exc:
                skills.append({"name": path.stem, "error": str(exc)})
    return {"skills": skills, "count": len(skills)}
