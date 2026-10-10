"""E25: crash acceptance of the durable runtime with real process kills.

A worker process runs the real execute_claimed_step against a synthetic
recipient process (tests/e25/recipient_app.py) that commits one effect row
through the real get_db and E24 fence. AIW_CRASH_BARRIER makes the worker
SIGKILL itself at a named point (app/utils/crash_barrier.py). The test then
expires the lease, reclaims, resumes the work in a fresh worker process and
counts effects, receipts and final states. Production workers are never
touched: everything runs in processes this test starts, on the test database.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Party,
    WorkEffectReceipt,
    WorkOrder,
    WorkStep,
    WorkStepAttempt,
    WorkToolCall,
)
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import (
    claim_ready_step,
    create_work_order,
    create_work_plan,
    reclaim_expired_leases,
    utcnow,
)

BACKEND = Path(__file__).resolve().parents[1]
SERVICE_KEY = "e25-service-key"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(BACKEND),
            "AGENT_SERVICE_KEY": SERVICE_KEY,
            "AUTH_ENABLED": "false",
        }
    )
    env.pop("AIW_CRASH_BARRIER", None)
    env.update(extra)
    return env


@pytest.fixture(scope="module")
def recipient():
    if not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("POSTGRES_HOST"):
        pytest.skip("E25 harness needs the test database reachable by child processes")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.e25.recipient_app:app", "--port", str(port)],
        cwd=BACKEND,
        env=_env(E25_SELF_URL=url),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("synthetic recipient did not start")
        yield url
    finally:
        proc.kill()
        proc.wait()


def _run_worker(recipient_url: str, step_id, attempt_id, barrier: str | None) -> int:
    extra = {"FASTAPI_URL": recipient_url}
    if barrier:
        extra["AIW_CRASH_BARRIER"] = barrier
    proc = subprocess.run(
        [sys.executable, "tests/e25/run_step.py", str(step_id), str(attempt_id)],
        cwd=BACKEND,
        env=_env(**extra),
        capture_output=True,
        timeout=120,
    )
    return proc.returncode


async def _claimed_order(factory, marker: str):
    async with factory() as db:
        order = await create_work_order(
            db, owner_key="e25-owner", objective=f"E25 {marker}", budgets={"max_replans": 3}
        )
        await initialize_budget_ledger(db, order.id)
        await create_work_plan(
            db,
            order,
            steps=[
                {
                    "step_key": "effect",
                    "kind": "capability",
                    # A real fenced one-commit operation: the reclaim decides by
                    # the catalog; the synthetic recipient answers every route.
                    "capability": "invoices",
                    "action": "update",
                    "input": {"marker": marker},
                    "max_attempts": 3,
                    "timeout_seconds": 60,
                }
            ],
            actor="e25",
        )
        _o, step, attempt = await claim_ready_step(db, worker_id="e25-w1", work_order_id=order.id)
        await db.commit()
        return order.id, step.id, attempt.id


async def _lose_and_reclaim(factory, order_id, step_id):
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        step.lease_expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    async with factory() as db:
        await reclaim_expired_leases(db)
        await db.commit()


async def _counts(factory, order_id, marker):
    async with factory() as db:
        effects = await db.scalar(
            select(func.count()).select_from(Party).where(Party.name == f"e25:{marker}")
        )
        receipts = await db.scalar(
            select(func.count())
            .select_from(WorkEffectReceipt)
            .where(WorkEffectReceipt.work_order_id == order_id)
        )
        order = await db.get(WorkOrder, order_id)
        calls = list(
            await db.scalars(
                select(WorkToolCall.status)
                .where(WorkToolCall.work_order_id == order_id)
                .order_by(WorkToolCall.created_at)
            )
        )
        return effects, receipts, order.status, (order.blocker or {}).get("code"), calls


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_killed_before_dispatch_retries_and_commits_once(recipient, factory):
    marker = uuid.uuid4().hex
    order_id, step_id, attempt_id = await _claimed_order(factory, marker)

    assert _run_worker(recipient, step_id, attempt_id, "before_dispatch") == -9
    assert (await _counts(factory, order_id, marker))[0] == 0

    await _lose_and_reclaim(factory, order_id, step_id)
    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="e25-w2", work_order_id=order_id)
        await db.commit()
    assert claimed is not None, "a step killed before dispatch must be retried"
    _o, _s, retry = claimed
    assert _run_worker(recipient, step_id, retry.id, None) == 0

    effects, receipts, _status, _code, calls = await _counts(factory, order_id, marker)
    assert (effects, receipts) == (1, 1)
    assert calls == ["failed", "succeeded"]


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier", ["after_response", "before_result_commit"])
async def test_killed_after_the_recipient_committed_never_repeats(recipient, factory, barrier):
    marker = uuid.uuid4().hex
    order_id, step_id, attempt_id = await _claimed_order(factory, marker)

    assert _run_worker(recipient, step_id, attempt_id, barrier) == -9
    assert (await _counts(factory, order_id, marker))[:2] == (1, 1)

    await _lose_and_reclaim(factory, order_id, step_id)
    async with factory() as db:
        assert await claim_ready_step(db, worker_id="e25-w2", work_order_id=order_id) is None
        step = await db.get(WorkStep, step_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        assert (step.state, attempt.status) == ("failed", "abandoned")

    effects, receipts, status, code, calls = await _counts(factory, order_id, marker)
    assert (effects, receipts) == (1, 1)
    assert calls == ["reconciled_happened"]
    assert status == "replanning" and code == "effect_recorded_without_result"


@pytest.mark.asyncio
async def test_a_late_commit_from_the_dead_attempt_is_refused(recipient, factory):
    """The worker is gone and its lease reclaimed before its request lands."""
    from app.auth.effect_fence import EFFECT_FENCE_HEADER, new_effect_fence

    marker = uuid.uuid4().hex
    order_id, step_id, attempt_id = await _claimed_order(factory, marker)
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        plan_id = step.plan_id
    await _lose_and_reclaim(factory, order_id, step_id)

    token = new_effect_fence(
        work_order_id=order_id,
        step_id=step_id,
        attempt_id=attempt_id,
        plan_id=plan_id,
        plan_revision=1,
        operation_key=f"tool:{attempt_id}:p1:post:late",
    )
    late = httpx.post(
        f"{recipient}/api/agent/cap/invoices",
        json={"marker": marker},
        headers={"X-API-Key": SERVICE_KEY, EFFECT_FENCE_HEADER: token},
        timeout=30,
    )
    assert late.status_code == 409
    # The order went back to "ready" after the reclaim; either way refused.
    assert late.json()["detail"]["reason"] in {"attempt_lease_lost", "work_order_not_live"}
    assert (await _counts(factory, order_id, marker))[:2] == (0, 0)
