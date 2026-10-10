"""E25: a worker that died with a tool call in flight must not cause a repeat.

The E24 receipt is written in the recipient's own commit, under the work
order lock, so after the attempt is abandoned: a receipt proves the effect
happened; no receipt for a fenced operation proves it did not; anything else
is unknown and waits for the owner.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.models import WorkEffectReceipt, WorkToolCall
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import (
    claim_ready_step,
    create_work_order,
    create_work_plan,
    reclaim_expired_leases,
    utcnow,
)
from app.domain.work_planning import _read_planner_snapshot


async def _dead_worker_with_call(db, capability, action, *, receipt=False):
    order = await create_work_order(
        db,
        owner_key="reclaim-owner",
        objective=f"{capability}.{action}",
        budgets={"max_replans": 3},
    )
    await initialize_budget_ledger(db, order.id)
    await create_work_plan(
        db,
        order,
        steps=[
            {
                "step_key": "act",
                "kind": "capability",
                "capability": capability,
                "action": action,
                "max_attempts": 3,
            }
        ],
        actor="test",
    )
    _o, step, attempt = await claim_ready_step(db, worker_id="w-dead", work_order_id=order.id)
    call = WorkToolCall(
        work_order_id=order.id,
        step_id=step.id,
        attempt_id=attempt.id,
        call_no=1,
        executor="capability",
        capability=capability,
        action=action,
        arguments={"x": 1},
        status="prepared",
        action_digest="0" * 64,
        idempotency_key=f"k-{attempt.id}",
    )
    db.add(call)
    if receipt:
        db.add(
            WorkEffectReceipt(
                operation_key=f"tool:{attempt.id}:p1:post:abc",
                work_order_id=order.id,
                step_id=step.id,
                attempt_id=attempt.id,
                method="POST",
                path="/api/x",
            )
        )
    step.lease_expires_at = utcnow() - timedelta(seconds=1)
    await db.flush()
    return order, step, call


@pytest.mark.asyncio
async def test_a_receipt_means_the_effect_happened_and_the_step_is_not_retried(db_session):
    order, step, call = await _dead_worker_with_call(db_session, "invoices", "update", receipt=True)

    await reclaim_expired_leases(db_session)

    assert call.status == "reconciled_happened"
    assert call.error["reconciliation"]["basis"] == "effect_receipt"
    assert step.state == "failed"
    assert order.status == "replanning"
    assert order.blocker["code"] == "effect_recorded_without_result"
    snapshot = await _read_planner_snapshot(db_session, order.id, lock_order=False)
    done = [c["output"] for c in snapshot.completed_context if c["step_key"] == "act"]
    assert done and done[0]["effect_happened"] is True
    assert done[0]["basis"] == "effect_receipt"
    assert done[0]["owner_confirmed_effect"] is False


@pytest.mark.asyncio
async def test_no_receipt_under_the_fence_means_no_effect_and_a_safe_retry(db_session):
    order, step, call = await _dead_worker_with_call(db_session, "invoices", "update")

    await reclaim_expired_leases(db_session)

    assert call.status == "failed"
    assert call.error["effect"] == "not_committed"
    assert step.state == "retry_wait"


@pytest.mark.asyncio
async def test_an_unfenced_effect_without_receipt_is_unknown(db_session):
    order, step, call = await _dead_worker_with_call(db_session, "invoices", "approve")

    await reclaim_expired_leases(db_session)

    assert call.status == "outcome_unknown"
    assert step.state == "failed"
    assert order.status == "blocked"
    assert order.blocker["code"] == "unresolved_effect_requires_reconciliation"
