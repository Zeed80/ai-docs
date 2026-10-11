"""The lease heartbeat and a checkpoint write never deadlock (E50 live run).

The checkpoint writer locks the work order, then inserts a journal row whose
foreign key needs KEY SHARE on the attempt. The heartbeat used to lock the
attempt FOR UPDATE and then wait for the order: Postgres found the cycle and
killed one of them. FOR NO KEY UPDATE on the attempt leaves the key check
free, so both finish.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.chat_runs import ChatRunCreate, submit_chat_run
from app.auth.jwt import _DEV_USER
from app.db.agent_runtime_models import ChatLogicalAction
from app.db.models import WorkOrder, WorkStep
from app.domain.work_orders import claim_ready_step
from app.tasks.work_orders import _heartbeat_step


@pytest.mark.asyncio
async def test_heartbeat_waits_for_a_checkpoint_instead_of_deadlocking(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        run = await submit_chat_run(
            ChatRunCreate(request_id=uuid.uuid4(), content="Deadlock probe"), db, _DEV_USER
        )
    async with factory() as db:
        _, step, attempt = await claim_ready_step(
            db, worker_id="deadlock-probe", work_order_id=run["work_order_id"]
        )
        await db.commit()
    order_id = run["work_order_id"]

    stop = asyncio.Event()
    checkpoint = factory()
    # The checkpoint writer holds the order lock first...
    await checkpoint.scalar(select(WorkOrder).where(WorkOrder.id == order_id).with_for_update())
    heartbeat = asyncio.create_task(
        _heartbeat_step(
            work_order_id=order_id,
            step_id=step.id,
            attempt_id=attempt.id,
            worker_id="deadlock-probe",
            stop=stop,
            interval_seconds=0,
            session_factory=factory,
        )
    )
    # ...the heartbeat takes the attempt and blocks on the order...
    await asyncio.sleep(0.5)
    # ...and the journal insert must still get its foreign-key check through.
    checkpoint.add(
        ChatLogicalAction(
            work_order_id=order_id,
            attempt_id=attempt.id,
            call_id="probe",
            request={"name": "x", "arguments": {}},
            request_digest="0" * 64,
            status="planned",
        )
    )
    await asyncio.wait_for(checkpoint.commit(), timeout=10)
    await checkpoint.close()
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(heartbeat, timeout=10)
    async with factory() as db:
        refreshed = await db.get(WorkStep, step.id)
        assert refreshed.lease_expires_at is not None
