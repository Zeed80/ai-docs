"""Fail-closed boundary for retired non-durable AgentSession execution."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import WorkOrder, WorkStep, WorkStepAttempt, WorkToolCall
from app.db.work_budget_models import WorkBudgetReservation
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import (
    claim_ready_step,
    create_single_step_plan,
    create_work_order,
)
from app.tasks import work_orders


@pytest_asyncio.fixture
async def headless_order_cleanup(test_engine):
    order_ids: list = []
    yield order_ids
    if not order_ids:
        return
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        ledger_ids = set(
            (
                await db.scalars(
                    select(WorkOrder.budget_ledger_id).where(
                        WorkOrder.id.in_(order_ids),
                        WorkOrder.budget_ledger_id.is_not(None),
                    )
                )
            ).all()
        )
        await db.execute(
            delete(WorkBudgetReservation).where(WorkBudgetReservation.work_order_id.in_(order_ids))
        )
        await db.execute(
            update(WorkOrder).where(WorkOrder.id.in_(order_ids)).values(budget_ledger_id=None)
        )
        if ledger_ids:
            from app.db.work_budget_models import WorkBudgetLedger

            await db.execute(
                delete(WorkBudgetReservation).where(WorkBudgetReservation.ledger_id.in_(ledger_ids))
            )
            await db.execute(
                delete(WorkBudgetLedger).where(
                    WorkBudgetLedger.id.in_(ledger_ids),
                    WorkBudgetLedger.root_work_order_id.in_(order_ids),
                )
            )
        await db.execute(delete(WorkOrder).where(WorkOrder.id.in_(order_ids)))
        await db.commit()


async def _claimed_headless(
    test_engine,
    cleanup_order_ids,
    *,
    source: str,
    bind_ledger: bool,
    runner: str | None = None,
):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(
            db,
            owner_key="headless-owner",
            objective="Run retired headless path",
            source=source,
            budgets={"max_replans": 3},
        )
        if bind_ledger:
            await initialize_budget_ledger(db, order.id)
        cleanup_order_ids.append(order.id)
        _, step = await create_single_step_plan(
            db,
            order,
            kind="agent_turn",
            title="Unsafe headless turn",
            input_data={
                "prompt": "Do not dispatch",
                **({"runner": runner} if runner is not None else {}),
            },
            max_attempts=3,
        )
        order_id, step_id = order.id, step.id
        await db.commit()
    async with factory() as db:
        claimed = await claim_ready_step(
            db,
            worker_id="headless-guard",
            work_order_id=order_id,
        )
        assert claimed is not None
        _, _, attempt = claimed
        attempt_id = attempt.id
        await db.commit()
    return factory, order_id, step_id, attempt_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "bind_ledger", "runner"),
    [
        ("api", True, None),
        ("legacy_agent_task", False, None),
        ("email", False, None),
        ("decompose", False, None),
        ("email_rule", False, None),
        # Step input is caller-controlled plan data.  It cannot forge the
        # server-owned WorkOrder provenance required by the durable executor.
        ("api", True, "durable_chat"),
    ],
)
async def test_non_durable_agent_turn_blocks_before_every_dispatch(
    test_engine, monkeypatch, headless_order_cleanup, source, bind_ledger, runner
):
    factory, order_id, step_id, attempt_id = await _claimed_headless(
        test_engine,
        headless_order_cleanup,
        source=source,
        bind_ledger=bind_ledger,
        runner=runner,
    )
    dispatch = AsyncMock(side_effect=AssertionError("model/tool dispatcher must not run"))
    monkeypatch.setattr(work_orders, "_execute_step_kind", dispatch)

    assert not await work_orders.execute_claimed_step(
        step_id,
        attempt_id,
        schedule_verification=False,
        session_factory=factory,
    )
    assert dispatch.await_count == 0

    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        step = await db.get(WorkStep, step_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        assert order.status == "blocked"
        assert order.blocker["code"] == "headless_agent_turn_requires_durable_intake"
        assert order.plan_revision == 1
        assert step.state == "failed"
        assert step.attempt_count == 1
        assert step.next_attempt_at is None
        assert attempt.status == "failed"
        assert attempt.error["code"] == "headless_agent_turn_requires_durable_intake"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkToolCall)
                .where(WorkToolCall.attempt_id == attempt_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkBudgetReservation)
                .where(WorkBudgetReservation.work_order_id == order_id)
            )
            == 0
        )

    # A repeated delivery cannot reset the blocked state or open a new attempt.
    assert not await work_orders.execute_claimed_step(
        step_id,
        attempt_id,
        schedule_verification=False,
        session_factory=factory,
    )
    assert dispatch.await_count == 0


@pytest.mark.asyncio
async def test_existing_headless_dispatch_marker_wins_over_new_guard(
    test_engine, monkeypatch, headless_order_cleanup
):
    factory, order_id, step_id, attempt_id = await _claimed_headless(
        test_engine,
        headless_order_cleanup,
        source="legacy_agent_task",
        bind_ledger=False,
    )
    async with factory() as db:
        db.add(
            WorkToolCall(
                work_order_id=order_id,
                step_id=step_id,
                attempt_id=attempt_id,
                call_no=1,
                executor="agent_turn",
                arguments={"prompt": "historic"},
                resolved_from={},
                status="running",
                action_digest="a" * 64,
                idempotency_key=f"historic:{attempt_id}",
                output={"recipient_outcome": "unknown"},
            )
        )
        await db.commit()
    dispatch = AsyncMock(side_effect=AssertionError("historic call must not replay"))
    monkeypatch.setattr(work_orders, "_execute_step_kind", dispatch)

    assert not await work_orders.execute_claimed_step(
        step_id,
        attempt_id,
        schedule_verification=False,
        session_factory=factory,
    )
    assert dispatch.await_count == 0
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        step = await db.get(WorkStep, step_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        call = await db.scalar(select(WorkToolCall).where(WorkToolCall.attempt_id == attempt_id))
        assert order.status == "running"
        assert order.blocker is None
        assert step.state == "running"
        assert attempt.status == "running"
        assert call.status == "running"
        assert call.output == {"recipient_outcome": "unknown"}
