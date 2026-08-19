"""Lease recovery and budget housekeeping — the periodic passes _dispatch_ready_work
runs before claiming new work (reclaim_expired_leases, enforce_budgets), plus a
concurrency test for claim_ready_step's SKIP LOCKED guarantee (Б18).

Split out of test_work_orders.py (Б18) — that file now covers CRUD/API-level
contracts and domain flows that don't fit lease/verifier/replanning/computer-use
buckets; see test_work_order_verifier.py, test_work_order_replanning.py,
test_computer_use_grants.py for the other three.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.work_orders import (
    claim_ready_step,
    complete_attempt,
    create_single_step_plan,
    create_work_order,
    enforce_budgets,
    reclaim_expired_leases,
)


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_retried(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Восстановиться после падения worker",
    )
    await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Execute",
        input_data={"prompt": order.objective},
        max_attempts=2,
    )
    claimed = await claim_ready_step(
        db_session, worker_id="dead-worker", work_order_id=order.id
    )
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    claimed_order.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    claimed_step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    reclaimed = await reclaim_expired_leases(db_session)

    assert reclaimed == 1
    assert claimed_order.status == "ready"
    assert claimed_step.state == "retry_wait"
    assert claimed_step.lease_owner is None
    assert attempt.status == "abandoned"


@pytest.mark.asyncio
async def test_enforce_budgets_blocks_order_that_overspent_token_budget(db_session):
    """Б15: a WorkOrder with an explicit token_budget gets blocked once the
    sum of its attempts' tokens_used exceeds it — not before, and not for
    orders with no token_budget set at all (default: unenforced, see A4/Б15
    on why unset stays unset rather than defaulting to some magic number)."""
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Дорогой ход, который надо остановить по бюджету",
        budgets={"token_budget": 100},
    )
    await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Execute",
        input_data={"prompt": order.objective},
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={"text": "готово", "tokens_used": {"input_tokens": 80, "output_tokens": 40}},
        actor="worker",
    )
    await db_session.flush()
    assert attempt.tokens_used == 120  # over the 100 budget, not blocked until enforce_budgets runs
    assert claimed_order.status == "running"  # complete_attempt alone doesn't check budgets

    blocked = await enforce_budgets(db_session)

    assert blocked == 1
    assert claimed_order.status == "blocked"
    assert claimed_order.blocker["code"] == "token_budget_exceeded"
    assert claimed_order.blocker["tokens_spent"] == 120


@pytest.mark.asyncio
async def test_enforce_budgets_ignores_orders_without_a_token_budget(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Без явного бюджета — не трогаем",
    )
    await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Execute",
        input_data={"prompt": order.objective},
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={"text": "готово", "tokens_used": {"input_tokens": 999999, "output_tokens": 999999}},
        actor="worker",
    )
    await db_session.flush()

    blocked = await enforce_budgets(db_session)

    assert blocked == 0
    assert claimed_order.status == "running"


@pytest.mark.asyncio
async def test_concurrent_claim_only_one_worker_gets_the_ready_step(test_engine):
    """Б18: N workers racing claim_ready_step over the same single ready step
    — claim_ready_step's ``with_for_update(skip_locked=True)`` must hand it to
    exactly one, never zero (a live step going unclaimed) and never more than
    one (two workers executing the same step). Needs real separate
    connections racing on the real database — db_session's single
    rolled-back transaction (used by every other test in this file) can't
    exercise cross-connection row locking, so this uses test_engine directly,
    like test_completed_work_order_materializes_provenance_memory_and_recipe
    in test_work_orders.py does for the same reason.
    """
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as setup_db:
        order = await create_work_order(
            setup_db, owner_key="tester", objective="Race for one step"
        )
        await create_single_step_plan(
            setup_db, order, kind="agent_turn", title="Execute", input_data={"prompt": "x"}
        )
        order_id = order.id
        await setup_db.commit()

    async def _try_claim(worker_id: str) -> bool:
        async with factory() as db:
            claimed = await claim_ready_step(db, worker_id=worker_id, work_order_id=order_id)
            await db.commit()
            return claimed is not None

    results = await asyncio.gather(*[_try_claim(f"worker-{i}") for i in range(6)])

    assert sum(results) == 1, f"expected exactly one winner, got {results}"
