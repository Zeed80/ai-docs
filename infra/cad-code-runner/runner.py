"""Isolated executor for VLM-generated CAD-reading code (ezdxf/numpy).

Runs in its own locked-down container (non-root, read-only fs, no secrets, no
network — see docker-compose's ``cad_sandbox`` internal network — resource
limits). The backend talks to it over HTTP:

- ``POST /execute`` — AST-gate the code against an IMPORT ALLOWLIST (not the
  blocklist ``capability_sandbox.py`` uses for promoted skills — this code is
  never reviewed by a human before running, once per CAD digitization, so the
  narrower rule is "prove it only does geometry", not "block known-bad
  modules"), then run it in a subprocess with a hard timeout (the
  container's own cgroup mem_limit bounds memory — see the module-level
  comment near EXECUTE_TIMEOUT_S for why a subprocess-level rlimit is not
  used here). The script's contract: its LAST stdout line must be
  ``json.dumps(result)``. If it wrote a ``.dxf`` file (via ``ezdxf``) in its
  scratch directory, that comes back base64-encoded too — for human/audit
  review only, nothing downstream parses it.
- ``GET /health`` — liveness.

Generated code executes ONLY here. It has no database, no object storage, no
service credentials and (per the compose network) no route to anything but
this container's own filesystem.
"""

from __future__ import annotations

import ast
import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI

app = FastAPI(title="cad-code-runner", docs_url=None, redoc_url=None)

EXECUTE_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 60.0
_MAX_DXF_BYTES = 2 * 1024 * 1024
# No subprocess-level RLIMIT_AS: live-verified 2026-08-15 that it makes even
# `import numpy` fail ("OpenBLAS error: Memory allocation still failed") —
# BLAS reserves virtual address space up front that RLIMIT_AS counts against
# even though it is never resident, so a limit tight enough to mean anything
# kills every script before it runs a single line. The container's own
# cgroup mem_limit (docker-compose) already bounds actual (resident) memory
# use correctly and is the real guard here.

# Only what a geometry-computation script legitimately needs. Anything else
# — os, sys, subprocess, socket, pathlib, importlib, ctypes, builtins-as-a-
# module, pickle, urllib, requests, http, ... — is rejected before the code
# is ever executed. This is deliberately an ALLOWLIST, not a blocklist: the
# code is never seen by a human before it runs, so the default must be
# refusal, not "block the modules we thought of".
_ALLOWED_IMPORT_ROOTS = frozenset({
    "ezdxf", "numpy", "math", "json", "dataclasses", "typing", "itertools",
})
# Builtins that reach outside pure computation even without an import.
_FORBIDDEN_CALLS = frozenset({
    "eval", "exec", "__import__", "compile", "execfile", "open", "input",
})


def _ast_gate(code: str) -> list[str]:
    """Return a list of rejection reasons; empty means the code may run."""
    errors: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"SyntaxError: {exc}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in _ALLOWED_IMPORT_ROOTS:
                    errors.append(f"Import not allowed: '{alias.name}'")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top not in _ALLOWED_IMPORT_ROOTS:
                errors.append(f"Import not allowed: 'from {node.module}'")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else ""
            )
            if name in _FORBIDDEN_CALLS:
                errors.append(f"Call not allowed: '{name}()'")
    return errors


def _collect_dxf(scratch: Path) -> str | None:
    """Base64 of the first (smallest-numbered) .dxf the script wrote, if any.

    Audit/review only — see module docstring. A script that writes more than
    one is unusual; picking the lexicographically first keeps this simple
    rather than guessing which one matters.
    """
    for candidate in sorted(scratch.glob("*.dxf")):
        try:
            data = candidate.read_bytes()
        except OSError:
            continue
        if 0 < len(data) <= _MAX_DXF_BYTES:
            return base64.b64encode(data).decode("ascii")
        return None
    return None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/execute")
async def execute(payload: dict[str, Any] = Body(...)) -> dict:
    code = str(payload.get("code") or "")
    if not code.strip():
        return {"ok": False, "error": "empty code", "result": None}

    timeout_s = payload.get("timeout_s")
    timeout_s = (
        min(float(timeout_s), MAX_TIMEOUT_S)
        if isinstance(timeout_s, (int, float)) and timeout_s > 0
        else EXECUTE_TIMEOUT_S
    )

    gate_errors = _ast_gate(code)
    if gate_errors:
        return {"ok": False, "error": "; ".join(gate_errors), "result": None}

    started = time.monotonic()
    with tempfile.TemporaryDirectory(dir="/tmp") as scratch_dir:
        scratch = Path(scratch_dir)
        script_path = scratch / "candidate.py"
        script_path.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=str(scratch),
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"execution timed out after {timeout_s:g}s",
                "result": None,
                "duration_s": round(time.monotonic() - started, 3),
            }
        except Exception as exc:  # noqa: BLE001 — infra failure, not code's fault
            return {"ok": False, "error": f"runner error: {exc}", "result": None}

        duration_s = round(time.monotonic() - started, 3)
        dxf_base64 = _collect_dxf(scratch)

        if proc.returncode != 0:
            return {
                "ok": False,
                "error": (proc.stderr or "")[-2000:] or f"exit code {proc.returncode}",
                "result": None,
                "stdout_tail": (proc.stdout or "")[-2000:],
                "duration_s": duration_s,
                "dxf_base64": dxf_base64,
            }

        lines = (proc.stdout or "").strip().splitlines()
        if not lines:
            return {
                "ok": False,
                "error": "script produced no stdout",
                "result": None,
                "duration_s": duration_s,
                "dxf_base64": dxf_base64,
            }
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "error": f"last stdout line is not valid JSON: {exc}",
                "result": None,
                "stdout_tail": (proc.stdout or "")[-2000:],
                "duration_s": duration_s,
                "dxf_base64": dxf_base64,
            }
        return {
            "ok": True,
            "error": None,
            "result": result,
            "duration_s": duration_s,
            "dxf_base64": dxf_base64,
        }
