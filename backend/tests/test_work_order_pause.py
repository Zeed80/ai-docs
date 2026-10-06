"""E22: pause at a safe boundary, resume without resetting the budget."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import WorkEvent, WorkOrder
from app.db.work_budget_models import WorkBudgetReservation
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import (
    WorkStateError,
    acknowledge_pause_requests,
    claim_ready_step,
    complete_attempt,
    create_work_order,
    create_work_plan,
    request_pause,
    resume_paused,
    transition_work_order,
)


def _steps(*keys):
    return [
        {
            "step_key": key,
            "title": key,
            "kind": "capability",
            "capability": "suppliers",
            "action": "list",
            "input": {},
        }
        for key in keys
    ]


async def _order(factory, *keys):
    async with factory() as db:
        order = await create_work_order(db, owner_key="pause-owner", objective="Пауза")
        await initialize_budget_ledger(db, order.id)
        await create_work_plan(db, order, steps=_steps(*keys), actor="test")
        await db.commit()
        return order.id


async def _events(db, order_id, kind):
    return await db.scalar(
        select(func.count())
        .select_from(WorkEvent)
        .where(WorkEvent.work_order_id == order_id, WorkEvent.event_type == kind)
    )


@pytest.mark.asyncio
async def test_pause_between_steps_is_immediate_and_resume_restores_work(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id = await _order(factory, "a")
    async with factory() as db:
        order = await db.get(WorkOrder, order_id, with_for_update=True)
        assert await request_pause(db, order, actor="owner")
        assert await request_pause(db, order, actor="owner")  # second request: no-op
        await db.commit()
        assert order.status == "paused"
        assert order.metadata_["pause"]["previous_status"] == "ready"
        assert await _events(db, order_id, "work.pause_requested") == 1
        assert await claim_ready_step(db, worker_id="w", work_order_id=order_id) is None

        await resume_paused(db, order, actor="owner")
        await db.commit()
        assert order.status == "ready"
        assert "pause" not in order.metadata_
        assert await claim_ready_step(db, worker_id="w", work_order_id=order_id) is not None


@pytest.mark.asyncio
async def test_pause_during_a_step_waits_for_it_and_claims_nothing_new(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id = await _order(factory, "a", "b")
    async with factory() as db:
        claimed_order, step, attempt = await claim_ready_step(
            db, worker_id="w1", work_order_id=order_id
        )
        await db.commit()
    async with factory() as db:
        order = await db.get(WorkOrder, order_id, with_for_update=True)
        assert not await request_pause(db, order, actor="owner")  # requested, not yet safe
        await db.commit()
        assert order.status == "running"
        assert await claim_ready_step(db, worker_id="w2", work_order_id=order_id) is None

    # The started step is finished, never treated as undone.
    async with factory() as db:
        order = await db.get(WorkOrder, order_id, with_for_update=True)
        await complete_attempt(
            db,
            order=order,
            step=await db.get(type(step), step.id),
            attempt=await db.get(type(attempt), attempt.id),
            output={"result": {"items": []}},
            actor="w1",
        )
        await db.commit()
    # A "restart" before acknowledgement: the intent is in the database and
    # the next dispatch tick acknowledges it.
    async with factory() as db:
        assert await acknowledge_pause_requests(db) == 1
        await db.commit()
        order = await db.get(WorkOrder, order_id)
        assert order.status == "paused"
        assert order.metadata_["pause"]["acknowledged_at"]


@pytest.mark.asyncio
async def test_resume_keeps_spent_budget_and_cancel_after_pause_works(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id = await _order(factory, "a")
    async with factory() as db:
        order = await db.get(WorkOrder, order_id, with_for_update=True)
        db.add(
            WorkBudgetReservation(
                ledger_id=order.budget_ledger_id,
                work_order_id=order_id,
                operation_key="llm:spent",
                dimension="llm_calls",
                reserved_units=1,
                actual_units=1,
                request_digest="0" * 64,
                binding_digest="0" * 64,
                state="charged",
            )
        )
        await request_pause(db, order, actor="owner")
        await resume_paused(db, order, actor="owner")
        await request_pause(db, order, actor="owner")
        await transition_work_order(db, order, "canceled", actor="owner")
        await db.commit()
        assert order.status == "canceled"
        spent = await db.scalar(
            select(func.count())
            .select_from(WorkBudgetReservation)
            .where(WorkBudgetReservation.work_order_id == order_id)
        )
        assert spent == 1


@pytest.mark.asyncio
async def test_a_pending_request_can_be_withdrawn_and_terminal_orders_refuse(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id = await _order(factory, "a")
    async with factory() as db:
        claim = await claim_ready_step(db, worker_id="w", work_order_id=order_id)
        await db.commit()
        order = claim[0]
        assert not await request_pause(db, order, actor="owner")
        await resume_paused(db, order, actor="owner")  # withdraws the request
        await db.commit()
        assert order.status == "running" and "pause" not in order.metadata_
        assert await _events(db, order_id, "work.pause_withdrawn") == 1

        await transition_work_order(db, order, "canceled", actor="owner")
        with pytest.raises(WorkStateError):
            await request_pause(db, order, actor="owner")


@pytest.mark.asyncio
async def test_pause_api_for_the_owner(client):
    created = await client.post("/api/work-orders", json={"objective": "Пауза через API"})
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]

    paused = await client.post(f"/api/work-orders/{order_id}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"
    resumed = await client.post(f"/api/work-orders/{order_id}/unpause")
    assert resumed.status_code == 200
    assert resumed.json()["status"] != "paused"
    again = await client.post(f"/api/work-orders/{order_id}/unpause")
    assert again.status_code == 409
