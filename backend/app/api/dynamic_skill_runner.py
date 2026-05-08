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

    try:
        result = await module.execute(args)
        logger.info("generated_skill_executed", skill=skill_name)
        return result
    except Exception as exc:
        logger.error("generated_skill_execution_failed", skill=skill_name, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Skill execution failed: {exc}")


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
