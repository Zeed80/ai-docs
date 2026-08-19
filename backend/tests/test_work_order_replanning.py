"""Bounded replanning after a terminal step failure — a failed step with no
attempts left routes the WorkOrder to replanning up to budgets.max_replans,
not an unbounded retry loop.

Split out of test_work_orders.py (Б18) — see that file's docstring for the
full split map.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import WorkPlan
from app.domain.work_orders import (
    claim_ready_step,
    create_single_step_plan,
    create_work_order,
    fail_attempt,
)


@pytest.mark.asyncio
async def test_terminal_step_failure_enters_bounded_replanning(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Recover",
        budgets={"max_replans": 1},
    )
    await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": "x"}, max_attempts=1
    )
    claimed = await claim_ready_step(db_session, worker_id="worker", work_order_id=order.id)
    assert claimed is not None
    claimed_order, step, attempt = claimed

    await fail_attempt(
        db_session,
        order=claimed_order,
        step=step,
        attempt=attempt,
        error={"code": "boom"},
        retryable=False,
        actor="worker",
    )
    assert claimed_order.status == "replanning"
    assert step.state == "failed"
    active = list(
        (
            await db_session.execute(
                select(WorkPlan).where(WorkPlan.work_order_id == order.id, WorkPlan.status == "active")
            )
        ).scalars()
    )
    assert len(active) == 1
