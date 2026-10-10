"""Agent script supervisor (E27): one fresh container per run, nothing else.

The only component with the Docker socket. It accepts a signed run request,
picks the runtime image from its own allowlist by image ID (never from the
request), runs it read-only without network under kernel limits, feeds the
files over stdin, reads the single result frame from the container log and
removes the container. No paths, commands or images come from the caller;
there is no shell endpoint. See docs/agent-employee-delivery/
script-isolation-contract.md.

Refuses instead of degrading: no Docker, no cgroup v2, an image missing or
not matching its pinned ID, an unknown runtime — the run is "refused" with a
reason, never executed elsewhere.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import socket
import tarfile
import threading
import time
from typing import Any

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

FRAME = "===AIW-SCRIPT-RESULT==="
LABEL = "aiw.script-run"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_FILES_BYTES = 20 * 1024 * 1024


def _settings() -> dict[str, Any]:
    return {
        "key": os.environ.get("SCRIPT_SUPERVISOR_KEY", ""),
        # {"python3.11": "sha256:<image id>"}: the only images that ever run.
        "runtimes": json.loads(os.environ.get("SCRIPT_RUNTIMES", "{}")),
        "oci_runtime": os.environ.get("SCRIPT_OCI_RUNTIME", "runc"),
    }


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=8, max_length=80)
    owner_key: str = Field(min_length=1, max_length=200)
    work_order_id: str
    step_id: str
    attempt_id: str
    runtime: str
    files: dict[str, str]  # name -> base64; must contain main.py
    timeout_seconds: int = DEFAULT_TIMEOUT
    extended_time_authorized: bool = False
    expires_at: int


class Supervisor:
    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    # ── environment checks ──────────────────────────────────────────────
    def environment_problem(self) -> str | None:
        try:
            info = self.client.info()
        except DockerException:
            return "docker_unavailable"
        if str(info.get("CgroupVersion")) != "2":
            return "cgroup_v2_required"
        runtimes = info.get("Runtimes") or {}
        if _settings()["oci_runtime"] not in runtimes:
            return "oci_runtime_unavailable"
        return None

    def resolve_image(self, runtime: str) -> tuple[str | None, str | None]:
        pinned = _settings()["runtimes"].get(runtime)
        if not pinned:
            return None, "runtime_not_allowed"
        try:
            image = self.client.images.get(pinned)
        except ImageNotFound:
            return None, "runtime_missing"
        if image.id != pinned:
            return None, "runtime_digest_mismatch"
        return image.id, None

    def cleanup_orphans(self) -> int:
        """After a supervisor restart nothing it started may keep running."""
        removed = 0
        for container in self.client.containers.list(all=True, filters={"label": LABEL}):
            try:
                container.remove(force=True)
                removed += 1
            except NotFound:
                pass
        return removed

    # ── one run ─────────────────────────────────────────────────────────
    def run(self, request: RunRequest) -> dict[str, Any]:
        with self._lock:
            previous = self._runs.get(request.run_id)
            if previous is not None:
                if previous["owner_key"] != request.owner_key:
                    raise HTTPException(404, "Run not found")
                return previous["result"]
            self._runs[request.run_id] = {
                "owner_key": request.owner_key,
                "result": {"status": "running"},
            }
        result = self._execute(request)
        with self._lock:
            entry = self._runs[request.run_id]
            if entry.get("canceled"):
                result = {**result, "status": "canceled"}
            entry["result"] = result
        return result

    def cancel(self, run_id: str, owner_key: str) -> dict[str, Any]:
        """Kill a run's container; its recorded result becomes "canceled"."""
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                # Canceled before the run reached us: a tombstone, so the
                # request arriving later is answered "canceled", not executed.
                entry = {"owner_key": owner_key, "result": {"status": "canceled"}}
                self._runs[run_id] = entry
        if entry["owner_key"] != owner_key:
            raise HTTPException(404, "Run not found")
        killed = 0
        for container in self.client.containers.list(
            all=True, filters={"label": f"{LABEL}={run_id}"}
        ):
            try:
                container.kill()
                killed += 1
            except (NotFound, DockerException):
                pass
        with self._lock:
            if entry["result"].get("status") == "running":
                entry["result"] = {"status": "canceled"}
            entry["canceled"] = True
        return {"killed": killed, **entry["result"]}

    def get(self, run_id: str, owner_key: str) -> dict[str, Any]:
        with self._lock:
            entry = self._runs.get(run_id)
        if entry is None or entry["owner_key"] != owner_key:
            raise HTTPException(404, "Run not found")
        return entry["result"]

    def _refused(self, reason: str) -> dict[str, Any]:
        return {"status": "refused", "reason": reason}

    def _execute(self, request: RunRequest) -> dict[str, Any]:
        problem = self.environment_problem()
        if problem:
            return self._refused(problem)
        image_id, problem = self.resolve_image(request.runtime)
        if problem:
            return self._refused(problem)
        if "main.py" not in request.files:
            return self._refused("main_py_missing")
        limit = MAX_TIMEOUT if request.extended_time_authorized else DEFAULT_TIMEOUT
        timeout = max(1, min(int(request.timeout_seconds), limit))
        archive = io.BytesIO()
        total = 0
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for name, encoded in request.files.items():
                if "/" in name or name.startswith("."):
                    return self._refused("file_name_not_allowed")
                data = base64.b64decode(encoded)
                total += len(data)
                if total > MAX_FILES_BYTES:
                    return self._refused("input_too_large")
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o444
                tar.addfile(info, io.BytesIO(data))
        api = self.client.api
        host_config = api.create_host_config(
            network_mode="none",
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=512m"},
            mem_limit="1g",
            memswap_limit="1g",
            nano_cpus=1_000_000_000,
            pids_limit=64,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            runtime=_settings()["oci_runtime"],
        )
        created = api.create_container(
            image_id,
            labels={LABEL: request.run_id, "aiw.owner": request.owner_key},
            environment={"AIW_TIMEOUT": str(timeout)},
            user="65534:65534",
            # No StdinOnce in the SDK: the bootstrap stops at the tar
            # end-of-archive marker, so it does not wait for EOF.
            stdin_open=True,
            network_disabled=True,
            host_config=host_config,
        )
        container = self.client.containers.get(created["Id"])
        started = time.time()
        try:
            stdin = container.attach_socket(params={"stdin": 1, "stream": 1})
            container.start()
            raw = getattr(stdin, "_sock", stdin)
            raw.sendall(archive.getvalue())
            raw.shutdown(socket.SHUT_WR)
            raw.close()
            status_override = None
            try:
                container.wait(timeout=timeout + 10)
            except Exception:  # noqa: BLE001 — the outer deadline
                container.kill()
                status_override = "timeout"
            state = self.client.api.inspect_container(container.id)["State"]
            logs = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
        finally:
            container.remove(force=True)
        finished = time.time()
        base = {
            "image_id": image_id,
            "oci_runtime": _settings()["oci_runtime"],
            "timeout_seconds": timeout,
            "duration_ms": int((finished - started) * 1000),
        }
        if state.get("OOMKilled"):
            return {**base, "status": "oom", "exit_code": state.get("ExitCode")}
        if status_override:
            return {**base, "status": status_override, "exit_code": state.get("ExitCode")}
        frame_line = next(
            (line for line in reversed(logs.splitlines()) if line.startswith(FRAME)), None
        )
        if frame_line is None:
            # Killed before the frame (OOM inside the bootstrap, kernel kill).
            code = state.get("ExitCode")
            return {**base, "status": "oom" if code == 137 else "failed", "exit_code": code}
        frame = json.loads(frame_line[len(FRAME) :])
        outputs = []
        with tarfile.open(
            fileobj=io.BytesIO(base64.b64decode(frame.pop("out_tar_b64"))), mode="r"
        ) as tar:
            for member in tar.getmembers():
                data = tar.extractfile(member).read() if member.isfile() else b""
                outputs.append(
                    {
                        "name": member.name,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "content_b64": base64.b64encode(data).decode(),
                    }
                )
        return {**base, **frame, "output_artifacts": outputs}


app = FastAPI(title="agent-script-supervisor", docs_url=None, redoc_url=None)
supervisor = Supervisor()


@app.on_event("startup")
def _startup() -> None:
    try:
        supervisor.cleanup_orphans()
    except DockerException:
        pass  # health reports docker_unavailable; runs are refused


def _verify(body: bytes, signature: str | None) -> None:
    key = _settings()["key"]
    if not key:
        raise HTTPException(503, "Supervisor key is not configured")
    expected = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Invalid run signature")


@app.get("/health")
def health() -> dict[str, Any]:
    problem = supervisor.environment_problem()
    runtimes = {name: supervisor.resolve_image(name)[1] or "ok" for name in _settings()["runtimes"]}
    return {"ok": problem is None, "problem": problem, "runtimes": runtimes}


@app.post("/runs")
async def create_run(
    request: Request, x_aiw_signature: str | None = Header(default=None)
) -> dict[str, Any]:
    body = await request.body()
    _verify(body, x_aiw_signature)
    run = RunRequest.model_validate_json(body)
    if run.expires_at < int(time.time()):
        raise HTTPException(401, "Run request expired")
    from starlette.concurrency import run_in_threadpool

    return {"run_id": run.run_id, **(await run_in_threadpool(supervisor.run, run))}


@app.get("/runs/{run_id}")
def get_run(run_id: str, owner_key: str, x_aiw_signature: str | None = Header(default=None)):
    _verify(f"{run_id}:{owner_key}".encode(), x_aiw_signature)
    return {"run_id": run_id, **supervisor.get(run_id, owner_key)}


@app.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, owner_key: str, x_aiw_signature: str | None = Header(default=None)):
    _verify(f"cancel:{run_id}:{owner_key}".encode(), x_aiw_signature)
    return {"run_id": run_id, **supervisor.cancel(run_id, owner_key)}
