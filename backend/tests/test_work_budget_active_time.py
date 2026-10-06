"""E21: active step time is charged to the shared ledger's active_seconds."""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import WorkOrder, WorkStep
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import claim_ready_step, create_work_order, create_work_plan
from app.tasks import work_orders as tasks


async def _claimed(factory, *, max_active_seconds=None, timeout=600):
    async with factory() as db:
        order = await create_work_order(db, owner_key="time-owner", objective="Время")
        ledger = await initialize_budget_ledger(db, order.id)
        if max_active_seconds is not None:
            ledger.max_active_seconds = Decimal(max_active_seconds)
        await create_work_plan(
            db,
            order,
            steps=[
                {
                    "step_key": "s",
                    "title": "s",
                    "kind": "capability",
                    "capability": "suppliers",
                    "action": "list",
                    "input": {},
                    "timeout_seconds": timeout,
                }
            ],
            actor="test",
        )
        await db.commit()
        _o, step, attempt = await claim_ready_step(db, worker_id="w", work_order_id=order.id)
        await db.commit()
        return order.id, ledger.id, step.id, attempt.id


async def _active(factory, order_id):
    async with factory() as db:
        return list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.work_order_id == order_id,
                    WorkBudgetReservation.dimension == "active_seconds",
                )
            )
        )


@pytest.mark.asyncio
async def test_step_reserves_its_cap_and_is_charged_the_measured_time(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id, _ledger, step_id, attempt_id = await _claimed(factory, timeout=900)
    executed = []

    async def fake_inner(*args, **kwargs):
        executed.append(True)
        [row] = await _active(factory, order_id)
        assert row.reserved_units == 700  # min(timeout 900, the Celery hard limit)
        return True

    monkeypatch.setattr(tasks, "_execute_claimed_step", fake_inner)
    assert await tasks.execute_claimed_step(step_id, attempt_id, session_factory=factory)

    [row] = await _active(factory, order_id)
    assert executed and row.state == "charged"
    assert Decimal(0) <= row.actual_units < Decimal(5)


@pytest.mark.asyncio
async def test_spent_budget_blocks_before_the_step_runs(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id, ledger_id, step_id, attempt_id = await _claimed(factory, max_active_seconds=60)
    async with factory() as db:
        ledger = await db.get(WorkBudgetLedger, ledger_id)
        db.add(
            WorkBudgetReservation(
                ledger_id=ledger.id,
                work_order_id=order_id,
                operation_key="active:earlier-step",
                dimension="active_seconds",
                reserved_units=60,
                actual_units=60,
                request_digest="0" * 64,
                binding_digest="0" * 64,
                state="charged",
            )
        )
        await db.commit()

    async def must_not_run(*args, **kwargs):
        pytest.fail("a step ran without active-time budget")

    monkeypatch.setattr(tasks, "_execute_claimed_step", must_not_run)
    assert not await tasks.execute_claimed_step(step_id, attempt_id, session_factory=factory)

    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        step = await db.get(WorkStep, step_id)
        assert order.status == "blocked"
        assert order.blocker["code"] == "active_time_budget_exceeded"
        assert step.state == "failed"


@pytest.mark.asyncio
async def test_reservation_never_exceeds_the_remaining_budget(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id, _ledger, step_id, attempt_id = await _claimed(factory, max_active_seconds=60)
    seen = []

    async def fake_inner(*args, **kwargs):
        seen.extend(row.reserved_units for row in await _active(factory, order_id))
        return True

    monkeypatch.setattr(tasks, "_execute_claimed_step", fake_inner)
    await tasks.execute_claimed_step(step_id, attempt_id, session_factory=factory)
    assert seen == [60]
