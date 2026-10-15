"""A step is claimed by the task that runs it, not by the dispatcher (2026-10-15).

On the shared IO worker a catalog import held every slot; a chat step the
dispatcher had already claimed waited in the queue past its 120 s lease and
failed "lease_expired" before the turn began.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import WorkStep
from app.domain.work_orders import create_single_step_plan, create_work_order, ready_step_ids
from app.tasks import work_orders as tasks


async def _ready_step(db) -> uuid.UUID:
    order = await create_work_order(db, owner_key="tester", objective="Как идут дела?")
    await create_single_step_plan(
        db, order, kind="agent_turn", title="Execute", input_data={"prompt": "x"}, max_attempts=2
    )
    await db.commit()
    return (
        await db.execute(select(WorkStep.id).where(WorkStep.work_order_id == order.id))
    ).scalar_one()


@pytest.mark.asyncio
async def test_dispatch_queues_without_claiming_and_only_once(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        step_id = await _ready_step(db)
        assert step_id in await ready_step_ids(db, limit=1000)

    queued: list[str] = []
    marks: set[str] = set()

    def mark(step):
        if str(step) in marks:
            return False
        marks.add(str(step))
        return True

    monkeypatch.setattr(tasks, "_mark_queued", mark)
    monkeypatch.setattr(
        tasks.claim_and_execute_step, "apply_async", lambda args, queue: queued.append(args[0])
    )
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)

    await tasks._dispatch_ready_work(limit=100)
    await tasks._dispatch_ready_work(limit=100)
    assert queued.count(str(step_id)) == 1  # the second pass saw the marker

    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        assert step.state in {"ready", "retry_wait"} and step.lease_owner is None  # no lease yet


@pytest.mark.asyncio
async def test_the_executing_task_claims_then_runs(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        step_id = await _ready_step(db)
    ran: list[tuple] = []

    async def execute(step, attempt, *, session_factory=None):
        ran.append((step, attempt))
        return True

    cleared: list[str] = []
    monkeypatch.setattr(tasks, "execute_claimed_step", execute)
    monkeypatch.setattr(tasks, "_clear_queued", lambda s: cleared.append(str(s)))
    assert await tasks.claim_and_execute(step_id, session_factory=factory) is True
    assert ran and ran[0][0] == step_id and cleared == [str(step_id)]
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        assert step.state == "running" and step.lease_owner
    # A second task for the same step finds nothing to claim.
    assert await tasks.claim_and_execute(step_id, session_factory=factory) is False
