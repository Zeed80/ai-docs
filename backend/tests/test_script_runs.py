"""E29: script runs through durable work with the real supervisor and Docker.

The supervisor (infra/agent-script-supervisor) runs as its own uvicorn
process; each run is a fresh container from the pinned runtime image.
Skipped when Docker or the runtime image is not available.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.agent_runtime_models import AgentScriptRun
from app.db.models import WorkArtifact, WorkOrder, WorkStep, WorkToolCall
from app.db.work_budget_models import WorkBudgetReservation
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import claim_ready_step, create_work_order, create_work_plan
from app.tasks.work_orders import execute_claimed_step

SUPERVISOR_DIR = Path(__file__).resolve().parents[2] / "infra" / "agent-script-supervisor"
KEY = "e29-key"


def _runtime_id() -> str | None:
    try:
        out = subprocess.run(
            ["docker", "images", "--no-trunc", "-q", "aiw-script-runtime:py311"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


@pytest.fixture(scope="module")
def supervisor_url():
    runtime_id = _runtime_id()
    if not runtime_id:
        pytest.skip("Docker or the aiw-script-runtime:py311 image is not available")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ)
    env.update(
        {
            "SCRIPT_SUPERVISOR_KEY": KEY,
            "SCRIPT_RUNTIMES": json.dumps({"python3.11": runtime_id}),
            "SCRIPT_OCI_RUNTIME": "runc",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--port", str(port)],
        cwd=SUPERVISOR_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                if httpx.get(f"{url}/health", timeout=1).json().get("ok"):
                    break
            except (httpx.HTTPError, ValueError):
                time.sleep(0.2)
        else:
            pytest.fail("supervisor did not start")
        yield url
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture
def enabled(monkeypatch, supervisor_url):
    monkeypatch.setattr(settings, "script_runs_enabled", True)
    monkeypatch.setattr(settings, "script_supervisor_url", supervisor_url)
    monkeypatch.setattr(settings, "script_supervisor_key", KEY)
    return supervisor_url


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def _script_step(factory, code: str, *, owner="script-owner", **extra):
    async with factory() as db:
        order = await create_work_order(db, owner_key=owner, objective="Script work")
        await initialize_budget_ledger(db, order.id)
        await create_work_plan(
            db,
            order,
            steps=[
                {
                    "step_key": "compute",
                    "kind": "script",
                    "input": {"code": code, **extra},
                    "max_attempts": 2,
                    "timeout_seconds": 120,
                }
            ],
            actor="owner",
        )
        _o, step, attempt = await claim_ready_step(db, worker_id="w1", work_order_id=order.id)
        await db.commit()
        return order.id, step.id, attempt.id


async def _execute(factory, step_id, attempt_id):
    return await execute_claimed_step(
        step_id, attempt_id, schedule_verification=False, session_factory=factory
    )


async def _run_row(factory, order_id) -> AgentScriptRun:
    async with factory() as db:
        return await db.scalar(
            select(AgentScriptRun).where(AgentScriptRun.work_order_id == str(order_id))
        )


@pytest.mark.asyncio
async def test_a_script_runs_with_journal_budget_and_registered_output(factory, enabled):
    code = "open('/tmp/out/r.txt','w').write('42')\nprint('ok')\n"
    order_id, step_id, attempt_id = await _script_step(factory, code)

    assert await _execute(factory, step_id, attempt_id)

    run = await _run_row(factory, order_id)
    assert run.status == "succeeded" and run.result["stdout"].strip() == "ok"
    [artifact] = run.output_artifact_ids
    assert artifact["sha256"] == hashlib.sha256(b"42").hexdigest()
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        assert step.state == "succeeded" and step.output["stdout"].strip() == "ok"
        call = await db.scalar(select(WorkToolCall).where(WorkToolCall.step_id == step_id))
        assert (call.executor, call.status) == ("script", "succeeded")
        registered = await db.get(WorkArtifact, uuid.UUID(artifact["id"]))
        assert registered.artifact_type == "script_output"
        charged = await db.scalar(
            select(func.count())
            .select_from(WorkBudgetReservation)
            .where(
                WorkBudgetReservation.work_order_id == order_id,
                WorkBudgetReservation.dimension == "tool_attempts",
                WorkBudgetReservation.state == "charged",
            )
        )
        assert charged == 1


@pytest.mark.asyncio
async def test_a_retried_attempt_reuses_the_run_instead_of_running_again(
    factory, enabled, monkeypatch
):
    """The worker finished the run but died before the step was recorded;
    the next attempt of the same step must not start a second job."""
    from app.domain.script_runs import run_script_step

    order_id, step_id, attempt_id = await _script_step(factory, "print('first')\n")
    assert await _execute(factory, step_id, attempt_id)
    # Unreachable supervisor: the retry must not need it.
    monkeypatch.setattr(settings, "script_supervisor_url", "http://127.0.0.1:9")

    output = await run_script_step(
        factory=factory,
        work_order_id=order_id,
        step_id=step_id,
        attempt_id=uuid.uuid4(),
        owner_key="script-owner",
        input_data={"code": "print('second')\n"},
        budget_context=None,
    )

    assert output["stdout"].strip() == "first"
    async with factory() as db:
        runs = await db.scalar(
            select(func.count())
            .select_from(AgentScriptRun)
            .where(AgentScriptRun.work_order_id == str(order_id))
        )
        assert runs == 1


@pytest.mark.asyncio
async def test_a_dead_workers_run_is_taken_over_from_the_supervisor(factory, enabled):
    from app.domain.script_runs import SupervisorClient

    order_id, step_id, attempt_id = await _script_step(factory, "print('second')\n")
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        run = AgentScriptRun(
            owner_key="script-owner",
            work_order_id=str(order_id),
            code="print('second')\n",
            status="running",
            logical_key=f"script:{step.idempotency_key}",
            evidence={},
        )
        db.add(run)
        await db.commit()
        run_id = run.id
    # The dead worker's job already finished at the supervisor.
    import base64

    await SupervisorClient().submit(
        {
            "run_id": str(run_id),
            "owner_key": "script-owner",
            "work_order_id": str(order_id),
            "step_id": str(step_id),
            "attempt_id": "dead",
            "runtime": "python3.11",
            "files": {"main.py": base64.b64encode(b"print('first-job')").decode()},
            "expires_at": int(time.time()) + 60,
        },
        timeout=60,
    )

    assert await _execute(factory, step_id, attempt_id)
    run = await _run_row(factory, order_id)
    assert run.result["stdout"].strip() == "first-job"  # taken over, not re-run


@pytest.mark.asyncio
async def test_disabled_scripts_never_reach_the_supervisor(factory, monkeypatch):
    monkeypatch.setattr(settings, "script_runs_enabled", False)
    order_id, step_id, attempt_id = await _script_step(factory, "print(1)\n")

    await _execute(factory, step_id, attempt_id)

    assert await _run_row(factory, order_id) is None
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        assert step.state == "failed"
        assert "script_runs_disabled" in str(step.last_error)


@pytest.mark.asyncio
async def test_an_unavailable_supervisor_is_a_refusal_not_a_fallback(factory, enabled, monkeypatch):
    monkeypatch.setattr(settings, "script_supervisor_url", "http://127.0.0.1:9")
    order_id, step_id, attempt_id = await _script_step(factory, "print(1)\n")

    await _execute(factory, step_id, attempt_id)

    run = await _run_row(factory, order_id)
    assert run.status == "refused"
    assert run.result["reason"].startswith("supervisor_unavailable")


@pytest.mark.asyncio
async def test_bobs_artifact_cannot_be_an_input(factory, enabled):
    async with factory() as db:
        bob = await create_work_order(db, owner_key="bob", objective="Bob's file")
        artifact = WorkArtifact(
            work_order_id=bob.id,
            artifact_type="upload",
            name="bob.txt",
            uri="artifacts/bob",
            content_hash="0" * 64,
        )
        db.add(artifact)
        await db.commit()
        artifact_id = artifact.id
    order_id, step_id, attempt_id = await _script_step(
        factory,
        "print(open('in.txt').read())\n",
        inputs=[{"artifact_id": str(artifact_id), "sha256": "0" * 64, "name": "in.txt"}],
    )

    await _execute(factory, step_id, attempt_id)

    run = await _run_row(factory, order_id)
    assert (run.status, run.result["reason"]) == ("refused", "artifact_not_found")


@pytest.mark.asyncio
async def test_a_run_over_its_time_is_timed_out(factory, enabled):
    order_id, step_id, attempt_id = await _script_step(
        factory, "import time\ntime.sleep(30)\n", timeout_seconds=2
    )

    await _execute(factory, step_id, attempt_id)

    run = await _run_row(factory, order_id)
    assert run.status == "timed_out"
    async with factory() as db:
        assert (await db.get(WorkStep, step_id)).state == "failed"


@pytest.mark.asyncio
async def test_canceling_the_order_kills_its_running_script(factory, enabled):
    from app.domain.script_runs import cancel_order_script_runs

    order_id, step_id, attempt_id = await _script_step(
        factory, "import time\ntime.sleep(60)\n", timeout_seconds=90
    )
    execution = asyncio.create_task(_execute(factory, step_id, attempt_id))
    for _ in range(100):
        run = await _run_row(factory, order_id)
        if run is not None and run.status == "running":
            break
        await asyncio.sleep(0.2)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        owner = order.owner_key
    started = time.time()
    assert await cancel_order_script_runs(factory, order_id, owner) == 1
    await asyncio.wait_for(execution, timeout=60)
    assert time.time() - started < 30
    run = await _run_row(factory, order_id)
    assert run.status == "canceled"


@pytest.mark.asyncio
async def test_data_from_earlier_steps_arrives_as_data_json(factory, enabled):
    code = (
        "import json\n"
        "rows = json.load(open('data.json'))['rows']\n"
        "print(sum(r['total'] for r in rows))\n"
    )
    order_id, step_id, attempt_id = await _script_step(
        factory, code, data={"rows": [{"total": 40}, {"total": 2}]}
    )

    assert await _execute(factory, step_id, attempt_id)

    run = await _run_row(factory, order_id)
    assert run.status == "succeeded" and run.result["stdout"].strip() == "42"


def _script_plan(code: str):
    from app.domain.work_planning import PlannedStep, PlannedWork

    return PlannedWork(
        steps=[PlannedStep(step_key="calc", title="calc", kind="script", input={"code": code})]
    )


def test_the_planner_may_emit_a_script_step_only_while_scripts_are_on(monkeypatch):
    from app.domain.work_planning import validate_capability_plan

    monkeypatch.setattr(settings, "script_runs_enabled", False)
    with pytest.raises(ValueError, match="switched off"):
        validate_capability_plan(_script_plan("print(1)"))
    monkeypatch.setattr(settings, "script_runs_enabled", True)
    assert validate_capability_plan(_script_plan("print(1)")).steps[0].kind == "script"


def test_script_code_cannot_carry_substituted_references():
    with pytest.raises(ValueError, match="input.data"):
        _script_plan("x = '${steps.lookup.output.result}'")
    with pytest.raises(ValueError, match="input.code"):
        _script_plan("")


@pytest.mark.asyncio
async def test_a_failed_script_tells_the_replan_what_it_saw(factory, enabled):
    code = (
        "import json, sys\n"
        "data = json.load(open('data.json'))\n"
        "if 'results' not in data:\n"
        "    print('expected results, found keys', sorted(data), file=sys.stderr)\n"
        "    sys.exit(2)\n"
    )
    order_id, step_id, attempt_id = await _script_step(
        factory, code, data={"items": [], "total": 0}
    )

    await _execute(factory, step_id, attempt_id)

    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        message = step.last_error["message"]
    assert message.startswith("script_run_failed: ")
    detail = json.loads(message.removeprefix("script_run_failed: "))
    assert detail["stderr_tail"].strip() == "expected results, found keys ['items', 'total']"
