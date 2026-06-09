"""Tests for capability sandbox AST security validation.

Verifies that _validate_draft rejects code with dangerous imports (os, subprocess, etc.)
and forbidden dynamic execution builtins (eval, exec, __import__, compile).
"""

import pytest

from app.ai.capability_sandbox import _validate_draft


def _draft(code: str) -> dict:
    return {
        "tool_name": "test_tool",
        "endpoint_path": "/api/test",
        "code": code,
        "implementation_plan": "test plan",
    }


VALID_CODE = """\
import json
import re

SKILL_META = {"name": "test_tool", "description": "Test"}


async def execute(args: dict) -> dict:
    value = args.get("value", "")
    return {"status": "ok", "result": json.dumps({"value": value})}
"""


# ── Safe code passes ──────────────────────────────────────────────────────────

def test_valid_code_passes():
    errors, _ = _validate_draft(_draft(VALID_CODE))
    assert not any("Forbidden" in e for e in errors), f"Safe code triggered errors: {errors}"


# ── Dangerous imports are blocked ─────────────────────────────────────────────

@pytest.mark.parametrize("forbidden_import", [
    "import os",
    "import sys",
    "import subprocess",
    "import socket",
    "import shutil",
    "import importlib",
    "import ctypes",
    "import pickle",
    "import builtins",
    "import pty",
    "import resource",
    "import signal",
    "import os.path",
    "import subprocess as sp",
])
def test_dangerous_import_blocked(forbidden_import: str):
    code = f"""\
{forbidden_import}
SKILL_META = {{"name": "evil"}}
async def execute(args): return {{}}
"""
    errors, _ = _validate_draft(_draft(code))
    assert any("Forbidden import" in e for e in errors), (
        f"Expected 'Forbidden import' error for `{forbidden_import}`, got: {errors}"
    )


@pytest.mark.parametrize("forbidden_from", [
    "from os import path",
    "from os.path import join",
    "from subprocess import run",
    "from sys import argv",
    "from shutil import copy",
    "from importlib import import_module",
])
def test_dangerous_from_import_blocked(forbidden_from: str):
    code = f"""\
{forbidden_from}
SKILL_META = {{"name": "evil"}}
async def execute(args): return {{}}
"""
    errors, _ = _validate_draft(_draft(code))
    assert any("Forbidden import" in e for e in errors), (
        f"Expected 'Forbidden import' error for `{forbidden_from}`, got: {errors}"
    )


# ── Dangerous builtins are blocked ────────────────────────────────────────────

@pytest.mark.parametrize("dangerous_call", [
    "eval(args['x'])",
    "exec('import os')",
    "__import__('os')",
    "compile('import os', '<string>', 'exec')",
    "execfile('/etc/passwd')",
])
def test_dangerous_call_blocked(dangerous_call: str):
    code = f"""\
SKILL_META = {{"name": "evil"}}
async def execute(args):
    return {dangerous_call}
"""
    errors, _ = _validate_draft(_draft(code))
    assert any("Forbidden call" in e for e in errors), (
        f"Expected 'Forbidden call' error for `{dangerous_call}`, got: {errors}"
    )


# ── Allowed imports still pass ────────────────────────────────────────────────

@pytest.mark.parametrize("safe_import", [
    "import json",
    "import re",
    "import uuid",
    "import datetime",
    "import math",
    "import collections",
    "import typing",
    "from typing import Any",
    "from datetime import datetime, timezone",
])
def test_safe_import_allowed(safe_import: str):
    code = f"""\
{safe_import}
SKILL_META = {{"name": "safe_tool"}}
async def execute(args): return {{"status": "ok"}}
"""
    errors, _ = _validate_draft(_draft(code))
    assert not any("Forbidden import" in e for e in errors), (
        f"Safe import `{safe_import}` was incorrectly blocked: {errors}"
    )
