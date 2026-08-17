"""Unit tests for the cad-code-runner AST allowlist gate and /execute
round-trip. ezdxf itself is not exercised here (not installed on the host
dev environment) — a real ezdxf script is verified live, inside the built
container, as part of the CAD reader's live verification, not here.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from runner import app, _ast_gate

client = TestClient(app)


def test_health() -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_ast_gate_allows_math_json_only_script() -> None:
    code = "import math\nimport json\nprint(json.dumps({'x': math.pi}))\n"
    assert _ast_gate(code) == []


def test_ast_gate_rejects_os_import() -> None:
    errors = _ast_gate("import os\nprint('hi')\n")
    assert errors and "os" in errors[0]


def test_ast_gate_rejects_subprocess_from_import() -> None:
    errors = _ast_gate("from subprocess import run\n")
    assert errors and "subprocess" in errors[0]


def test_ast_gate_rejects_eval_call() -> None:
    errors = _ast_gate("eval('1+1')\n")
    assert any("eval" in e for e in errors)


def test_ast_gate_rejects_open_call() -> None:
    errors = _ast_gate("open('/etc/passwd')\n")
    assert any("open" in e for e in errors)


def test_ast_gate_rejects_dunder_import_call() -> None:
    errors = _ast_gate("__import__('os')\n")
    assert any("__import__" in e for e in errors)


def test_ast_gate_allows_ezdxf_and_numpy_submodules() -> None:
    code = "import ezdxf\nimport ezdxf.math\nimport numpy as np\nprint('{}')\n"
    assert _ast_gate(code) == []


def test_execute_rejects_forbidden_import_before_running() -> None:
    resp = client.post("/execute", json={"code": "import socket\n"})
    body = resp.json()
    assert body["ok"] is False
    assert "socket" in body["error"]
    assert body["result"] is None


def test_execute_runs_allowed_code_and_parses_last_stdout_line() -> None:
    code = (
        "import json\n"
        "print('some diagnostic line')\n"
        "print(json.dumps({'outer': [{'diameter_mm': 80, 'length_mm': 100}]}))\n"
    )
    resp = client.post("/execute", json={"code": code, "timeout_s": 5})
    body = resp.json()
    assert body["ok"] is True, body
    assert body["result"] == {"outer": [{"diameter_mm": 80, "length_mm": 100}]}


def test_execute_reports_non_json_last_line() -> None:
    resp = client.post(
        "/execute", json={"code": "print('not json')\n", "timeout_s": 5}
    )
    body = resp.json()
    assert body["ok"] is False
    assert "JSON" in body["error"]


def test_execute_reports_python_exception() -> None:
    resp = client.post(
        "/execute", json={"code": "raise ValueError('boom')\n", "timeout_s": 5}
    )
    body = resp.json()
    assert body["ok"] is False
    assert "boom" in body["error"] or "ValueError" in body["error"]


def test_execute_kills_infinite_loop_on_timeout() -> None:
    resp = client.post(
        "/execute", json={"code": "while True:\n    pass\n", "timeout_s": 1}
    )
    body = resp.json()
    assert body["ok"] is False
    assert "timed out" in body["error"]


def test_execute_empty_code() -> None:
    resp = client.post("/execute", json={"code": ""})
    body = resp.json()
    assert body["ok"] is False
    assert "empty" in body["error"]


def test_execute_caps_timeout_at_max() -> None:
    resp = client.post(
        "/execute",
        json={"code": "print('{}')\n", "timeout_s": 999999},
    )
    assert resp.json()["ok"] is True
