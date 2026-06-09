"""Phase 5.2: capability sandbox smoke test (isolated subprocess load).

Covers what static AST validation cannot: that the generated module actually
imports and exposes a callable ``execute``. Runs in a time-bounded subprocess.
"""

from __future__ import annotations

from app.ai.capability_sandbox import _validate_draft


def _draft(code: str) -> dict:
    return {
        "tool_name": "smoke_tool",
        "endpoint_path": "/api/smoke",
        "code": code,
        "implementation_plan": "plan",
    }


def test_loadable_code_passes_smoke():
    code = (
        "import json\n"
        "SKILL_META = {'name': 'smoke_tool'}\n"
        "async def execute(args: dict) -> dict:\n"
        "    return {'status': 'ok', 'echo': json.dumps(args)}\n"
    )
    errors, _ = _validate_draft(_draft(code))
    assert not errors, f"Loadable code should pass: {errors}"


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


def test_dangerous_import_blocked_before_smoke():
    # AST gate must reject this; smoke must NOT run on already-rejected code.
    code = (
        "import os\n"
        "async def execute(args: dict) -> dict:\n"
        "    return {'status': 'ok'}\n"
    )
    errors, _ = _validate_draft(_draft(code))
    assert any("Forbidden import" in e for e in errors), errors
    assert not any("Smoke test" in e for e in errors), (
        "Smoke test must be skipped when AST validation already failed"
    )
