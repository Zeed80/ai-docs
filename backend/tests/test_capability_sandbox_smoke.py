"""Capability sandbox smoke test — now executed in the isolated skill-runner.

Covers what static AST validation cannot: that the generated module actually
imports and exposes a callable ``execute``. The backend no longer imports
candidate code itself — it POSTs it to the skill-runner's /smoke endpoint.
These tests simulate a healthy runner with a local stub that performs the
same import-check the real runner does (in a time-bounded subprocess).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from app.ai import capability_sandbox
from app.ai.capability_sandbox import _validate_draft

_HARNESS = (
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
    "print(json.dumps({'ok': not errs, 'errors': errs}))\n"
)


def _fake_runner_smoke(code: str) -> dict:
    """Same import-check the real skill-runner performs in its container."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as cf:
        cf.write(code)
        code_path = cf.name
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as hf:
        hf.write(_HARNESS)
        harness_path = hf.name
    proc = subprocess.run(
        [sys.executable, harness_path, code_path],
        capture_output=True, text=True, timeout=10,
    )
    lines = (proc.stdout or "").strip().splitlines()
    return json.loads(lines[-1]) if lines else {"ok": False, "errors": ["no smoke output"]}


@pytest.fixture(autouse=True)
def fake_runner(monkeypatch):
    """Route capability_sandbox smoke HTTP calls to the local stub."""
    calls: list[str] = []

    def fake_post(url, json=None, timeout=None):  # noqa: A002
        calls.append(url)
        assert "/smoke" in url
        payload = _fake_runner_smoke((json or {}).get("code") or "")
        return SimpleNamespace(status_code=200, content=b"x", json=lambda: payload)

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    yield calls


def _draft(code: str) -> dict:
    return {
        "tool_name": "smoke_tool",
        "endpoint_path": "/api/smoke",
        "code": code,
        "implementation_plan": "plan",
    }


def test_loadable_code_passes_smoke(fake_runner):
    code = (
        "import json\n"
        "SKILL_META = {'name': 'smoke_tool'}\n"
        "async def execute(args: dict) -> dict:\n"
        "    return {'status': 'ok', 'echo': json.dumps(args)}\n"
    )
    errors, _ = _validate_draft(_draft(code))
    assert not errors, f"Loadable code should pass: {errors}"
    assert fake_runner, "smoke must be delegated to the skill-runner"


def test_module_level_error_caught_by_smoke():
    # Valid syntax, has execute, no dangerous imports → AST passes.
    # But references an undefined name at import time → smoke must catch it.
    code = (
        "SKILL_META = {'name': 'smoke_tool'}\n"
        "VALUE = undefined_name_at_module_level\n"
        "async def execute(args: dict) -> dict:\n"
        "    return {'status': 'ok'}\n"
    )
    errors, _ = _validate_draft(_draft(code))
    assert any("Smoke test" in e for e in errors), errors
    assert any("NameError" in e for e in errors), errors


def test_missing_import_caught_by_smoke():
    code = (
        "import totally_missing_module_xyz\n"
        "SKILL_META = {'name': 'smoke_tool'}\n"
        "async def execute(args: dict) -> dict:\n"
        "    return {'status': 'ok'}\n"
    )
    errors, _ = _validate_draft(_draft(code))
    assert any("Smoke test" in e for e in errors), errors


def test_dangerous_import_blocked_before_smoke(fake_runner):
    # AST gate must reject this; smoke must NOT run on already-rejected code.
    code = (
        "import os\n"
        "async def execute(args: dict) -> dict:\n"
        "    return {'status': 'ok'}\n"
    )
    errors, _ = _validate_draft(_draft(code))
    assert any("Forbidden import" in e for e in errors), errors
    assert not fake_runner, "Smoke must be skipped when AST validation already failed"


def test_runner_unavailable_degrades_to_warning(monkeypatch):
    """Runner down → smoke becomes a warning, never a local import."""
    import httpx

    def boom(url, json=None, timeout=None):  # noqa: A002
        raise httpx.ConnectError("runner down")

    monkeypatch.setattr(httpx, "post", boom)
    errors, warnings = capability_sandbox._smoke_test_code(
        "async def execute(a): return {}"
    )
    assert not errors
    assert any("static validation only" in w for w in warnings)
