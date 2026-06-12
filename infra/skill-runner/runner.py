"""Isolated executor for agent-generated skills.

Runs in its own locked-down container (non-root, read-only fs, no secrets,
resource limits — see docker-compose). The backend talks to it over HTTP:

- ``POST /run/{skill_name}``  — import /skills/{skill_name}.py, await
  ``execute(args)`` with a hard timeout, return the result.
- ``POST /smoke``             — import-check a code candidate in a subprocess.
- ``GET /health``             — liveness.

Generated code executes ONLY here. It has no database, no object storage and
no service credentials: anything it needs from the system must go through the
backend's public HTTP API, where normal auth applies.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException

SKILLS_ROOT = Path("/skills")
EXECUTE_TIMEOUT_S = 30.0
SMOKE_TIMEOUT_S = 5.0

app = FastAPI(title="skill-runner", docs_url=None, redoc_url=None)


def _load_module(skill_name: str):
    safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in skill_name)
    path = SKILLS_ROOT / f"{safe}.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{safe}' not found")
    spec = importlib.util.spec_from_file_location(f"generated.{safe}", path)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=500, detail=f"Cannot load spec for {safe}")
    module = importlib.util.module_from_spec(spec)
    # Fresh import every call: generated files may be replaced between calls.
    sys.modules[f"generated.{safe}"] = module
    spec.loader.exec_module(module)
    return module


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/run/{skill_name}")
async def run_skill(skill_name: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
    args = payload.get("args") or {}
    try:
        module = _load_module(skill_name)
    except HTTPException:
        raise
    except Exception as exc:
        return {"status": "error", "skill": skill_name, "message": f"load failed: {exc}"}

    execute = getattr(module, "execute", None)
    if not callable(execute):
        return {"status": "error", "skill": skill_name, "message": "no execute() function"}

    try:
        result = execute(args)
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=EXECUTE_TIMEOUT_S)
    except NotImplementedError:
        return {
            "status": "error",
            "skill": skill_name,
            "message": "Skill is a stub and not yet implemented",
        }
    except asyncio.TimeoutError:
        return {"status": "error", "skill": skill_name, "message": "execution timed out"}
    except Exception as exc:
        return {"status": "error", "skill": skill_name, "message": str(exc)}

    if not isinstance(result, dict):
        return {"status": "ok", "skill": skill_name, "data": result}
    return result


@app.post("/smoke")
async def smoke(payload: dict[str, Any] = Body(...)) -> dict:
    """Import-check a code candidate in a subprocess (time-bounded)."""
    code = str(payload.get("code") or "")
    if not code.strip():
        return {"ok": False, "errors": ["empty code"]}

    harness = (
        "import importlib.util, json, sys\n"
        "path = sys.argv[1]\n"
        "try:\n"
        "    spec = importlib.util.spec_from_file_location('sandbox_candidate', path)\n"
        "    mod = importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(mod)\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok': False, 'errors': [f'{type(e).__name__}: {e}']}))\n"
        "    sys.exit(0)\n"
        "errs = []\n"
        "ex = getattr(mod, 'execute', None)\n"
        "if not callable(ex):\n"
        "    errs.append('execute is not callable after import')\n"
        "meta = getattr(mod, 'SKILL_META', None)\n"
        "if meta is not None and not isinstance(meta, dict):\n"
        "    errs.append('SKILL_META is not a dict')\n"
        "print(json.dumps({'ok': not errs, 'errors': errs}))\n"
    )
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir="/tmp") as cf:
            cf.write(code)
            code_path = cf.name
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir="/tmp") as hf:
            hf.write(harness)
            harness_path = hf.name
        proc = subprocess.run(
            [sys.executable, harness_path, code_path],
            capture_output=True,
            text=True,
            timeout=SMOKE_TIMEOUT_S,
        )
        lines = (proc.stdout or "").strip().splitlines()
        if not lines:
            return {"ok": False, "errors": ["no smoke output"]}
        return json.loads(lines[-1])
    except subprocess.TimeoutExpired:
        return {"ok": False, "errors": ["smoke test timed out"]}
    except Exception as exc:
        return {"ok": False, "errors": [f"smoke infrastructure error: {exc}"]}
