"""E27: the supervisor against the real Docker of this host.

Run: cd infra/agent-script-supervisor && python3 -m pytest tests -q
Needs the runtime image: docker build -t aiw-script-runtime:py311 runtime/
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
import uuid
from pathlib import Path

import docker
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as supervisor_app

KEY = "e27-test-key"


@pytest.fixture(scope="module")
def runtime_id() -> str:
    try:
        return docker.from_env().images.get("aiw-script-runtime:py311").id
    except Exception:  # noqa: BLE001
        pytest.skip("runtime image aiw-script-runtime:py311 is not built")


@pytest.fixture
def client(monkeypatch, runtime_id):
    monkeypatch.setenv("SCRIPT_SUPERVISOR_KEY", KEY)
    monkeypatch.setenv("SCRIPT_RUNTIMES", json.dumps({"python3.11": runtime_id}))
    monkeypatch.setenv("SCRIPT_OCI_RUNTIME", "runc")
    supervisor_app.supervisor = supervisor_app.Supervisor()
    with TestClient(supervisor_app.app) as test_client:
        yield test_client


def _sign(body: bytes) -> str:
    return hmac.new(KEY.encode(), body, hashlib.sha256).hexdigest()


def _run(client, code: str, *, owner="owner-a", run_id=None, **extra):
    payload = {
        "run_id": run_id or uuid.uuid4().hex,
        "owner_key": owner,
        "work_order_id": "wo",
        "step_id": "st",
        "attempt_id": "at",
        "runtime": "python3.11",
        "files": {"main.py": base64.b64encode(code.encode()).decode()},
        "expires_at": int(time.time()) + 60,
        **extra,
    }
    body = json.dumps(payload).encode()
    return client.post("/runs", content=body, headers={"X-AIW-Signature": _sign(body)})


def test_a_run_returns_bounded_logs_and_hashed_artifacts(client):
    code = "open('/tmp/out/result.txt','w').write('42')\nprint('hello')\nprint('x' * 100000)\n"
    response = _run(client, code)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "succeeded" and result["exit_code"] == 0
    assert result["stdout"].startswith("hello") and result["stdout_truncated"] is True
    [artifact] = result["output_artifacts"]
    assert artifact["name"] == "result.txt"
    assert artifact["sha256"] == hashlib.sha256(b"42").hexdigest()


def test_the_code_has_no_network_no_writable_root_and_no_privileges(client):
    code = (
        "import os, socket\n"
        "s = socket.socket(); s.settimeout(2)\n"
        "try:\n    s.connect(('1.1.1.1', 80)); print('NET')\n"
        "except OSError:\n    print('no-net')\n"
        "try:\n    open('/etc/x', 'w'); print('ROOT-WRITE')\n"
        "except OSError:\n    print('ro-root')\n"
        "print(os.getuid(), open('/proc/self/status').read().split('CapEff:')[1].split()[0])\n"
        "print(sorted(k for k in os.environ if 'KEY' in k or 'SECRET' in k))\n"
    )
    lines = _run(client, code).json()["stdout"].split()
    assert lines[:4] == ["no-net", "ro-root", "65534", "0000000000000000"]
    assert lines[4] == "[]"


def test_a_run_over_its_time_is_stopped(client):
    result = _run(client, "import time\ntime.sleep(30)\n", timeout_seconds=2).json()
    assert result["status"] == "timeout"
    assert result["duration_ms"] < 20000


def test_extra_time_needs_an_explicit_authorization(client):
    result = _run(client, "print(1)\n", timeout_seconds=500).json()
    assert result["timeout_seconds"] == 60
    authorized = _run(client, "print(1)\n", timeout_seconds=500, extended_time_authorized=True)
    assert authorized.json()["timeout_seconds"] == 500


def test_memory_over_the_limit_is_an_oom(client):
    code = "b = []\nwhile True:\n    b.append(bytearray(64 * 1024 * 1024))\n"
    assert _run(client, code).json()["status"] == "oom"


@pytest.mark.parametrize(
    "runtimes, reason",
    [
        ({}, "runtime_not_allowed"),
        ({"python3.11": "sha256:" + "0" * 64}, "runtime_missing"),
        ({"python3.11": "python:3.11-slim"}, "runtime_digest_mismatch"),
    ],
)
def test_an_unpinned_or_absent_runtime_is_refused(client, monkeypatch, runtimes, reason):
    monkeypatch.setenv("SCRIPT_RUNTIMES", json.dumps(runtimes))
    result = _run(client, "print(1)\n").json()
    assert result == {"run_id": result["run_id"], "status": "refused", "reason": reason}


def test_without_cgroup_v2_nothing_runs(client, monkeypatch):
    real_info = supervisor_app.supervisor.client.info
    monkeypatch.setattr(
        supervisor_app.supervisor.client, "info", lambda: {**real_info(), "CgroupVersion": "1"}
    )
    result = _run(client, "print(1)\n").json()
    assert (result["status"], result["reason"]) == ("refused", "cgroup_v2_required")


def test_a_restarted_supervisor_removes_what_it_left_running(runtime_id):
    engine = docker.from_env()
    orphan = engine.containers.run(
        "python:3.11-slim",
        ["python", "-c", "import time; time.sleep(300)"],
        labels={supervisor_app.LABEL: "orphan-run"},
        network_mode="none",
        detach=True,
    )
    try:
        assert supervisor_app.Supervisor(engine).cleanup_orphans() >= 1
        assert not engine.containers.list(all=True, filters={"label": supervisor_app.LABEL})
    finally:
        try:
            orphan.remove(force=True)
        except docker.errors.NotFound:
            pass


def test_another_owners_run_id_is_not_found(client):
    run_id = uuid.uuid4().hex
    assert _run(client, "print('mine')\n", run_id=run_id).json()["status"] == "succeeded"

    path = f"{run_id}:owner-b".encode()
    foreign = client.get(
        f"/runs/{run_id}", params={"owner_key": "owner-b"}, headers={"X-AIW-Signature": _sign(path)}
    )
    assert foreign.status_code == 404
    reused = _run(client, "print('theirs')\n", owner="owner-b", run_id=run_id)
    assert reused.status_code == 404
    # The same owner resubmitting gets the recorded result, not a second run.
    again = _run(client, "print('again')\n", run_id=run_id).json()
    assert again["stdout"].strip() == "mine"


def test_an_unsigned_or_expired_request_is_rejected(client):
    body = json.dumps({"run_id": "x" * 8}).encode()
    assert client.post("/runs", content=body).status_code == 401
    expired = _run(client, "print(1)\n", expires_at=int(time.time()) - 1)
    assert expired.status_code == 401


def test_a_canceled_run_is_killed_and_recorded_as_canceled(client):
    import threading

    run_id = uuid.uuid4().hex
    holder = {}
    worker = threading.Thread(
        target=lambda: holder.update(
            response=_run(client, "import time\ntime.sleep(60)\n", run_id=run_id)
        )
    )
    worker.start()
    engine = docker.from_env()
    for _ in range(100):
        if engine.containers.list(filters={"label": f"{supervisor_app.LABEL}={run_id}"}):
            break
        time.sleep(0.1)
    path = f"cancel:{run_id}:owner-a".encode()
    canceled = client.post(
        f"/runs/{run_id}/cancel",
        params={"owner_key": "owner-a"},
        headers={"X-AIW-Signature": _sign(path)},
    )
    assert canceled.status_code == 200, canceled.text
    worker.join(timeout=30)
    assert holder["response"].json()["status"] == "canceled"
    foreign = client.post(
        f"/runs/{run_id}/cancel",
        params={"owner_key": "owner-b"},
        headers={"X-AIW-Signature": _sign(f"cancel:{run_id}:owner-b".encode())},
    )
    assert foreign.status_code == 404


def test_a_cancel_before_the_run_arrives_prevents_it(client):
    run_id = uuid.uuid4().hex
    path = f"cancel:{run_id}:owner-a".encode()
    early = client.post(
        f"/runs/{run_id}/cancel",
        params={"owner_key": "owner-a"},
        headers={"X-AIW-Signature": _sign(path)},
    )
    assert early.status_code == 200
    late = _run(client, "open('/tmp/out/x','w').write('ran')\n", run_id=run_id).json()
    assert late["status"] == "canceled"
    assert "output_artifacts" not in late
