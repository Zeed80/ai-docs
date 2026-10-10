"""E29: lifecycle of an isolated script run inside durable work.

A "script" step (built by the owner; the planner cannot emit one) runs here:

1. The intent is committed first — an ``AgentScriptRun`` keyed by the step's
   logical key — so a retried attempt finds the run instead of starting a
   second job.
2. A finished run is reused, never re-executed. A run left "running" by a
   dead worker is reconciled with the supervisor: its recorded result is
   taken over; a run the supervisor no longer knows was killed with its
   container, and since the sandbox has no network and no shared storage,
   resubmitting it cannot repeat an effect.
3. Inputs come through the broker (E28); the supervisor call is one tool
   attempt on the shared budget ledger.
4. Outputs are registered only from a succeeded run and only while this
   attempt still holds its lease, in the same commit as the run's result.

Off until Gate C: ``settings.script_runs_enabled``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import select

from app.config import settings
from app.db.agent_runtime_models import AgentScriptRun
from app.db.models import WorkStep, WorkStepAttempt
from app.domain.script_broker import (
    BrokerRefused,
    ScriptInputRef,
    materialize_inputs,
    register_outputs,
)

_FINISHED = frozenset({"succeeded", "failed", "timed_out", "refused", "canceled"})
_STATUS = {
    "succeeded": "succeeded",
    "failed": "failed",
    "timeout": "timed_out",
    "oom": "failed",
    "refused": "refused",
    "canceled": "canceled",
}


class ScriptRunError(RuntimeError):
    """A run that ended without success: the step fails for good."""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


class ScriptRunPending(TimeoutError):
    """The supervisor still runs it: the step retries later, no new job."""


class SupervisorClient:
    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = (url or settings.script_supervisor_url).rstrip("/")
        self.key = key if key is not None else settings.script_supervisor_key
        self._transport = transport

    def _sign(self, data: bytes) -> str:
        return hmac.new(self.key.encode(), data, hashlib.sha256).hexdigest()

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    async def submit(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        body = json.dumps(payload).encode()
        async with self._client(timeout) as client:
            response = await client.post(
                f"{self.url}/runs",
                content=body,
                headers={"X-AIW-Signature": self._sign(body), "Content-Type": "application/json"},
            )
        response.raise_for_status()
        return response.json()

    async def get(self, run_id: str, owner_key: str) -> dict[str, Any] | None:
        async with self._client(30) as client:
            response = await client.get(
                f"{self.url}/runs/{run_id}",
                params={"owner_key": owner_key},
                headers={"X-AIW-Signature": self._sign(f"{run_id}:{owner_key}".encode())},
            )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def cancel(self, run_id: str, owner_key: str) -> None:
        async with self._client(30) as client:
            await client.post(
                f"{self.url}/runs/{run_id}/cancel",
                params={"owner_key": owner_key},
                headers={"X-AIW-Signature": self._sign(f"cancel:{run_id}:{owner_key}".encode())},
            )


def _bounded_result(remote: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "status",
        "exit_code",
        "stdout",
        "stdout_truncated",
        "stderr",
        "stderr_truncated",
        "image_id",
        "oci_runtime",
        "timeout_seconds",
        "duration_ms",
        "reason",
    )
    return {key: remote[key] for key in keep if key in remote}


def _output(run: AgentScriptRun) -> dict[str, Any]:
    result = run.result or {}
    return {
        "executor": "script",
        "run_id": str(run.id),
        "status": run.status,
        "exit_code": result.get("exit_code"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "output_artifacts": run.output_artifact_ids or [],
        "text": (result.get("stdout") or "")[:8000],
    }


def _finish(run: AgentScriptRun) -> dict[str, Any]:
    if run.status == "succeeded":
        return _output(run)
    raise ScriptRunError(
        f"script_run_{run.status}",
        {"run_id": str(run.id), "reason": (run.result or {}).get("reason")},
    )


async def run_script_step(
    *,
    factory: Any,
    work_order_id: uuid.UUID,
    step_id: uuid.UUID,
    attempt_id: uuid.UUID,
    owner_key: str,
    input_data: dict[str, Any],
    budget_context: Any | None,
    client: SupervisorClient | None = None,
) -> dict[str, Any]:
    if not settings.script_runs_enabled:
        raise ScriptRunError("script_runs_disabled")
    client = client or SupervisorClient()
    code = str(input_data.get("code") or "")
    if not code:
        raise ScriptRunError("script_code_missing")
    runtime = str(input_data.get("runtime") or "python3.11")
    refs = [ScriptInputRef.model_validate(item) for item in input_data.get("inputs") or []]
    timeout = int(input_data.get("timeout_seconds") or 60)

    # 1. Intent before any job: one run per step, across attempts.
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        logical_key = f"script:{step.idempotency_key}"
        run = await db.scalar(
            select(AgentScriptRun)
            .where(AgentScriptRun.logical_key == logical_key)
            .with_for_update()
        )
        if run is None:
            run = AgentScriptRun(
                owner_key=owner_key,
                work_order_id=str(work_order_id),
                code=code,
                code_sha256=hashlib.sha256(code.encode()).hexdigest(),
                status="queued",
                logical_key=logical_key,
                step_id=str(step_id),
                attempt_id=str(attempt_id),
                runtime=runtime,
                inputs=[ref.model_dump(mode="json") for ref in refs],
                timeout_seconds=timeout,
                evidence={},
            )
            db.add(run)
        else:
            run.attempt_id = str(attempt_id)
        await db.commit()
        run_id, status = run.id, run.status

    # 2. A finished run is reused; an in-flight one is reconciled.
    if status in _FINISHED:
        return _finish(run)
    if status in {"running", "unknown"}:
        remote = await client.get(str(run_id), owner_key)
        if remote is not None and remote.get("status") == "running":
            raise ScriptRunPending("script_run_still_running")
        if remote is not None:
            return await _record(factory, run_id, attempt_id, step_id, remote)
        # Unknown to the supervisor: it was killed with its container. No
        # network, no shared storage — nothing outside it can have happened.

    # 3. Inputs, then one budgeted call to the supervisor.
    async with factory() as db:
        try:
            files = await materialize_inputs(db, owner_key=owner_key, refs=refs)
        except BrokerRefused as refused:
            return await _record(
                factory,
                run_id,
                attempt_id,
                step_id,
                {"status": "refused", "reason": refused.reason},
            )
        locked = await db.get(AgentScriptRun, run_id, with_for_update=True)
        locked.status = "running"
        locked.started_at = locked.started_at or _now()
        await db.commit()
    payload = {
        "run_id": str(run_id),
        "owner_key": owner_key,
        "work_order_id": str(work_order_id),
        "step_id": str(step_id),
        "attempt_id": str(attempt_id),
        "runtime": runtime,
        "files": {
            "main.py": base64.b64encode(code.encode()).decode(),
            **{name: base64.b64encode(data).decode() for name, data in files.items()},
        },
        "timeout_seconds": timeout,
        "extended_time_authorized": bool(input_data.get("extended_time_authorized")),
        "expires_at": int(time.time()) + 120,
    }
    operation_key = None
    if budget_context is not None:
        operation_key = await budget_context.prepare_tool_attempt(
            method="POST",
            url=f"{client.url}/runs",
            request={"run_id": str(run_id), "code_sha256": run.code_sha256},
        )
    outcome = "unconfirmed"
    try:
        remote = await client.submit(payload, timeout=timeout + 60)
        outcome = "responded"
    except httpx.HTTPError as exc:
        # The supervisor is the only place code may run: no fallback.
        remote = {"status": "refused", "reason": f"supervisor_unavailable:{type(exc).__name__}"}
    finally:
        if operation_key is not None:
            await budget_context.charge_tool_attempt(operation_key, recipient_outcome=outcome)
    return await _record(factory, run_id, attempt_id, step_id, remote)


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def _record(
    factory: Any,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    step_id: uuid.UUID,
    remote: dict[str, Any],
) -> dict[str, Any]:
    from app.domain.work_orders import attempt_owns_lease

    async with factory() as db:
        run = await db.get(AgentScriptRun, run_id, with_for_update=True)
        step = await db.get(WorkStep, step_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        if run.status in _FINISHED:
            await db.rollback()
            return _finish(run)
        run.status = _STATUS.get(str(remote.get("status")), "unknown")
        run.result = _bounded_result(remote)
        run.finished_at = _now()
        current = step is not None and attempt is not None and attempt_owns_lease(step, attempt)
        if run.status == "succeeded" and remote.get("output_artifacts"):
            if current:
                rows = await register_outputs(
                    db,
                    work_order_id=uuid.UUID(run.work_order_id),
                    step_id=step_id,
                    run_id=str(run_id),
                    outputs=remote["output_artifacts"],
                )
                run.output_artifact_ids = [
                    {"id": str(row.id), "name": row.name, "sha256": row.content_hash}
                    for row in rows
                ]
            else:
                # Outputs publish only under the attempt that still holds the
                # lease; a stale worker records the run, not its files.
                run.evidence = {**(run.evidence or {}), "outputs_withheld": "attempt_not_current"}
        await db.commit()
        return _finish(run)


async def mark_script_runs_canceled(db: Any, work_order_id: uuid.UUID) -> list[str]:
    """In the cancel's own transaction: the order's live runs become canceled."""
    runs = list(
        await db.scalars(
            select(AgentScriptRun)
            .where(
                AgentScriptRun.work_order_id == str(work_order_id),
                AgentScriptRun.status.in_(["queued", "running"]),
            )
            .with_for_update()
        )
    )
    for run in runs:
        run.status = "canceled"
        run.finished_at = _now()
    return [str(run.id) for run in runs]


async def kill_script_runs(run_ids: list[str], owner_key: str) -> None:
    """After the cancel committed: kill their containers (or pre-empt them)."""
    client = SupervisorClient()
    for run_id in run_ids:
        try:
            await client.cancel(run_id, owner_key)
        except httpx.HTTPError:
            pass


async def cancel_order_script_runs(factory: Any, work_order_id: uuid.UUID, owner_key: str) -> int:
    """Both steps for callers without a transaction of their own."""
    async with factory() as db:
        run_ids = await mark_script_runs_canceled(db, work_order_id)
        await db.commit()
    await kill_script_runs(run_ids, owner_key)
    return len(run_ids)
