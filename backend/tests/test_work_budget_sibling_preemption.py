"""A sibling step stopped before dispatch must not turn a replan into a block."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import WorkOrder, WorkStep
from app.db.work_budget_models import WorkBudgetReservation
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import (
    claim_ready_step,
    create_work_order,
    create_work_plan,
    transition_work_order,
)
from app.tasks.work_orders import execute_claimed_step


async def _two_parallel_steps(factory):
    async with factory() as db:
        order = await create_work_order(db, owner_key="sib-owner", objective="Два шага")
        await initialize_budget_ledger(db, order.id)
        await create_work_plan(
            db,
            order,
            steps=[
                {
                    "step_key": key,
                    "title": key,
                    "kind": "capability",
                    "capability": "suppliers",
                    "action": "list",
                    "input": {},
                }
                for key in ("a", "b")
            ],
            actor="test",
        )
        await db.commit()
        first = await claim_ready_step(db, worker_id="w1", work_order_id=order.id)
        await db.commit()
        second = await claim_ready_step(db, worker_id="w2", work_order_id=order.id)
        await db.commit()
        return order.id, first, second


def _sibling_fails_mid_execution(monkeypatch, factory, order_id):
    """The first step fails while the second is already executing."""
    from app.tasks import work_orders as tasks

    original = tasks._execute_capability

    async def racing(*args, **kwargs):
        async with factory() as db:
            locked = await db.get(WorkOrder, order_id, with_for_update=True)
            await transition_work_order(db, locked, "replanning", actor="w1")
            await db.commit()
        return await original(*args, **kwargs)

    monkeypatch.setattr(tasks, "_execute_capability", racing)


@pytest.mark.asyncio
async def test_preempted_sibling_fails_only_its_step(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    order_id, _first, (_o, step, attempt) = await _two_parallel_steps(factory)
    _sibling_fails_mid_execution(monkeypatch, factory, order_id)

    await execute_claimed_step(
        step.id, attempt.id, schedule_verification=False, session_factory=factory
    )

    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        row = await db.get(WorkStep, step.id)
        assert order.status == "replanning"
        assert order.blocker is None
        assert row.state == "failed"
        assert row.last_error["code"] == "budget_execution_inactive"


@pytest.mark.asyncio
async def test_a_reserved_dispatch_still_blocks_conservatively(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    order_id, _first, (_o, step, attempt) = await _two_parallel_steps(factory)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        db.add(
            WorkBudgetReservation(
                ledger_id=order.budget_ledger_id,
                work_order_id=order_id,
                operation_key=f"tool:{attempt.id}:p1:post:abc",
                dimension="tool_attempts",
                reserved_units=1,
                request_digest="0" * 64,
                binding_digest="0" * 64,
                state="reserved",
            )
        )
        await db.commit()
    _sibling_fails_mid_execution(monkeypatch, factory, order_id)

    await execute_claimed_step(
        step.id, attempt.id, schedule_verification=False, session_factory=factory
    )

    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        assert order.status == "blocked"
        assert order.blocker["code"] == "budget_execution_inactive"


@pytest.mark.asyncio
async def test_reverifying_a_done_step_keeps_a_running_sibling_alive(test_engine):
    """Periodic re-verification must not flip the order to ready mid-step."""
    from app.domain.work_orders import promote_ready_dependents

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id, (_o1, done, _a1), (_o2, running, _a2) = await _two_parallel_steps(factory)
    async with factory() as db:
        step = await db.get(WorkStep, done.id)
        step.state = "succeeded"
        await db.commit()
    async with factory() as db:
        order = await db.get(WorkOrder, order_id, with_for_update=True)
        assert order.status == "running"
        assert await promote_ready_dependents(db, order=order, plan_id=done.plan_id)
        await db.commit()
        assert order.status == "running"  # the other step is still executing


@pytest.mark.asyncio
async def test_a_model_call_alone_does_not_block_a_preempted_step(test_engine, monkeypatch):
    """E23b live run: an owner instruction arrived after a synthesize step's
    model call returned; the order was blocked instead of replanned. A model
    call has no external effect to reconcile."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    order_id, _first, (_o, step, attempt) = await _two_parallel_steps(factory)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        db.add(
            WorkBudgetReservation(
                ledger_id=order.budget_ledger_id,
                work_order_id=order_id,
                operation_key=f"llm:{attempt.id}:synthesize:abc",
                dimension="llm_calls",
                reserved_units=1,
                request_digest="0" * 64,
                binding_digest="0" * 64,
                state="reserved",
            )
        )
        await db.commit()
    _sibling_fails_mid_execution(monkeypatch, factory, order_id)

    await execute_claimed_step(
        step.id, attempt.id, schedule_verification=False, session_factory=factory
    )

    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        row = await db.get(WorkStep, step.id)
        assert order.status == "replanning"
        assert order.blocker is None
        assert row.state == "failed"
