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
    _max_replans_for,
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


# ── Ф4 (AGENT_AUTONOMY_ROADMAP.md, user feedback 2026-08-20): exploratory ──
# orders get a much larger default max_replans than bounded ones, because
# replanning is their continue-working mechanism (Ф1.A), not just error
# recovery — the same small default that's right for a bounded task silently
# strangled persistence for an open-ended one.


def test_max_replans_for_defaults_higher_for_exploratory_orders_without_an_explicit_budget():
    class _Order:
        constraints = {"mode": "exploratory"}
        budgets = None

    assert _max_replans_for(_Order()) == 30


def test_max_replans_for_defaults_low_for_bounded_orders_without_an_explicit_budget():
    class _Order:
        constraints = {}
        budgets = None

    assert _max_replans_for(_Order()) == 2


def test_max_replans_for_explicit_budget_wins_regardless_of_mode():
    class _ExploratoryWithExplicitBudget:
        constraints = {"mode": "exploratory"}
        budgets = {"max_replans": 3}

    class _BoundedWithExplicitBudget:
        constraints = {}
        budgets = {"max_replans": 10}

    assert _max_replans_for(_ExploratoryWithExplicitBudget()) == 3
    assert _max_replans_for(_BoundedWithExplicitBudget()) == 10


@pytest.mark.asyncio
async def test_exploratory_order_survives_far_more_replans_than_the_bounded_default(db_session):
    """The regression this whole fix is for: a bounded order with no explicit
    max_replans is blocked after the 3rd plan revision (default 2), but an
    otherwise-identical exploratory order keeps replanning well past that —
    it must not be confused with "giving up" just because a plan revision
    counter crossed the old universal default."""
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Keep trying — exploratory, no explicit max_replans",
        constraints={"mode": "exploratory"},
    )
    for _ in range(5):
        await create_single_step_plan(
            db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": "x"}, max_attempts=1
        )
        claimed = await claim_ready_step(db_session, worker_id="worker", work_order_id=order.id)
        assert claimed is not None
        claimed_order, step, attempt = claimed
        await fail_attempt(
            db_session, order=claimed_order, step=step, attempt=attempt,
            error={"code": "boom"}, retryable=False, actor="worker",
        )
        # A bounded order (default max_replans=2) would already be "blocked"
        # well before plan_revision reaches 5 — this one must still be
        # replanning, i.e. still trying.
        assert claimed_order.status == "replanning", (
            f"exploratory order gave up at plan_revision={claimed_order.plan_revision} "
            "instead of continuing to replan"
        )
