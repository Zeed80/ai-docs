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

from app.db.models import WorkToolCall
from app.domain.work_orders import (
    claim_ready_step,
    complete_attempt,
    create_single_step_plan,
    create_work_order,
    create_work_plan,
    enforce_budgets,
    fail_attempt,
    find_active_plan_succeeded_steps,
    promote_ready_dependents,
    reclaim_expired_leases,
    transition_work_order,
    unstick_ready_orders_with_stalled_active_plan,
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
async def test_fail_attempt_clears_step_lease_so_retry_is_claimable_before_the_stale_lease_expires(
    db_session,
):
    """Found while adding Ф1.B checkpoint: fail_attempt used to leave the
    step's lease_owner/lease_expires_at from the original claim untouched, so
    a step that failed and entered retry_wait stayed unclaimable until that
    stale ~120s lease expired on its own — however short the computed
    exponential backoff said the retry should actually wait. Only
    reclaim_expired_leases (the *other* path into retry_wait, after a worker
    vanishes) cleared it. This asserts claim_ready_step succeeds once
    next_attempt_at has passed, without waiting for the original lease too.
    """
    order = await create_work_order(
        db_session, owner_key="tester", objective="Быстрый ретрай не должен ждать старую аренду"
    )
    await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Execute",
        input_data={"prompt": order.objective},
        max_attempts=2,
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    assert claimed_step.lease_expires_at is not None  # the original 120s claim lease

    await fail_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        error={"code": "execution_error", "message": "transient"},
        retryable=True,
        actor="w1",
    )
    await db_session.flush()

    assert claimed_step.state == "retry_wait"
    assert claimed_step.lease_owner is None
    assert claimed_step.lease_expires_at is None
    # Force the backoff delay itself to have passed — that's not what this
    # test is about, only whether the stale lease still blocks the claim.
    claimed_step.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    reclaimed = await claim_ready_step(db_session, worker_id="w2", work_order_id=order.id)

    assert reclaimed is not None
    _reclaimed_order, reclaimed_step, reclaimed_attempt = reclaimed
    assert reclaimed_step.id == claimed_step.id
    assert reclaimed_attempt.attempt_no == 2


@pytest.mark.asyncio
async def test_reclaim_expired_leases_still_blocks_order_when_it_already_left_running(
    db_session,
):
    """Ф4 (AGENT_AUTONOMY_ROADMAP.md): found live on the Ф4 pilot WorkOrder.
    reclaim_expired_leases used to gate its order-level transition on
    `order.status == "running"` — but by the time a claimed step's lease
    actually expires (>=120s later), a *sibling* step can easily have
    already succeeded and flipped the order back to "ready" in the
    meantime (promote_ready_dependents). The permanently-failed step's
    order.blocker got recorded, but the order's status silently never
    changed — a step with a pending dependent left the whole WorkOrder
    stuck in "ready" forever (claim_ready_step finds nothing new to claim,
    nothing re-evaluates it). Reproduces the exact shape: two independent
    top-level steps (one succeeds, flipping the order back to "ready" via
    promote_ready_dependents) plus a third step depending on the succeeded
    one, and the failing sibling's lease then expiring while the order is
    already "ready", not "running".
    """
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Deadlock repro: sibling succeeds first, then a lease expires while order is ready",
    )
    plan, steps = await create_work_plan(
        db_session,
        order,
        steps=[
            {
                "step_key": "create_supplier_haltec",
                "title": "Create supplier A",
                "kind": "agent_turn",
                "input": {"prompt": "a"},
                "depends_on": [],
                "success_predicate": {"type": "no_error_and_nonempty_output"},
                "max_attempts": 1,
            },
            {
                "step_key": "create_supplier_betar",
                "title": "Create supplier B",
                "kind": "agent_turn",
                "input": {"prompt": "b"},
                "depends_on": [],
                "success_predicate": {"type": "no_error_and_nonempty_output"},
                "max_attempts": 1,
            },
            {
                "step_key": "ingest_haltec_data",
                "title": "Ingest supplier A",
                "kind": "agent_turn",
                "input": {"prompt": "c"},
                "depends_on": ["create_supplier_haltec"],
                "success_predicate": {"type": "no_error_and_nonempty_output"},
                "max_attempts": 1,
            },
        ],
    )
    step_a = next(s for s in steps if s.step_key == "create_supplier_haltec")
    step_b = next(s for s in steps if s.step_key == "create_supplier_betar")

    claimed_a = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed_a is not None
    order_a, claimed_step_a, attempt_a = claimed_a
    assert claimed_step_a.id == step_a.id

    claimed_b = await claim_ready_step(db_session, worker_id="w2", work_order_id=order.id)
    assert claimed_b is not None
    order_b, claimed_step_b, attempt_b = claimed_b
    assert claimed_step_b.id == step_b.id
    assert order_b.status == "running"

    # step A succeeds; promote_ready_dependents (invoked the same way
    # verify_completed_step invokes it) frees the dependent step and flips
    # the order back to "ready" while B is still running.
    await complete_attempt(
        db_session, order=order_a, step=claimed_step_a, attempt=attempt_a,
        output={"text": "ok"}, actor="w1",
    )
    await promote_ready_dependents(db_session, order=order_a, plan_id=plan.id, actor="scheduler")
    await db_session.flush()
    assert order_a.status == "ready"

    # step B's worker vanishes; its lease expires while the order sits at "ready".
    claimed_step_b.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    reclaimed = await reclaim_expired_leases(db_session)

    assert reclaimed == 1
    assert claimed_step_b.state == "failed"
    assert order_a.blocker is not None
    assert order_a.status in {"replanning", "blocked"}, (
        "order must not stay stuck in 'ready' with an unreachable dependent step "
        f"(ingest_haltec_data can never lose its permanently-failed sibling as a "
        f"blocker for the order as a whole); got status={order_a.status!r}"
    )


@pytest.mark.asyncio
async def test_find_active_plan_succeeded_steps_excludes_superseded_plans(db_session):
    """Ф4 (AGENT_AUTONOMY_ROADMAP.md): found live on the Ф4 pilot — the
    verify_work_step dispatch query used to have no WorkPlan filter, so a
    long-running exploratory order that replanned kept re-selecting
    already-succeeded steps from *superseded* plans, whose re-verification
    then corrupted the order's live status mid-flight of the currently
    running step of the *actually active* plan (see
    find_active_plan_succeeded_steps' own docstring for the exact
    mechanism observed on the pilot). This is the regression test for the
    fix: a succeeded step from a superseded plan must never show up here
    while a different step from the active plan is genuinely running.
    """
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Replanned order must not re-surface superseded-plan steps",
    )
    old_plan, old_steps = await create_work_plan(
        db_session,
        order,
        steps=[
            {
                "step_key": "old_done",
                "title": "old done",
                "kind": "agent_turn",
                "input": {"prompt": "a"},
                "depends_on": [],
                "success_predicate": {"type": "no_error_and_nonempty_output"},
                "max_attempts": 1,
            },
            {
                "step_key": "old_stuck",
                "title": "old stuck",
                "kind": "agent_turn",
                "input": {"prompt": "b"},
                "depends_on": [],
                "success_predicate": {"type": "no_error_and_nonempty_output"},
                "max_attempts": 1,
            },
        ],
    )
    old_done = next(s for s in old_steps if s.step_key == "old_done")

    claimed_old = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed_old is not None
    order_ref, claimed_old_step, old_attempt = claimed_old
    assert claimed_old_step.id == old_done.id
    await complete_attempt(
        db_session, order=order_ref, step=claimed_old_step, attempt=old_attempt,
        output={"text": "ok"}, actor="w1",
    )
    # old_stuck is left in "ready", never claimed — old_plan stays "unfinished"
    # forever, exactly like a permanently-failed sibling would.
    await db_session.flush()

    # Replan: old_plan becomes superseded, a fresh active plan takes over —
    # mirrors reclaim_expired_leases/fail_attempt sending the order through
    # "replanning" before create_work_plan builds the next revision.
    await transition_work_order(db_session, order_ref, "replanning", actor="test")
    new_plan, new_steps = await create_work_plan(
        db_session,
        order,
        steps=[
            {
                "step_key": "current",
                "title": "current",
                "kind": "agent_turn",
                "input": {"prompt": "c"},
                "depends_on": [],
                "success_predicate": {"type": "no_error_and_nonempty_output"},
                "max_attempts": 1,
            },
        ],
    )
    assert old_plan.status == "superseded"
    assert new_plan.status == "active"

    claimed_new = await claim_ready_step(db_session, worker_id="w2", work_order_id=order.id)
    assert claimed_new is not None
    order_ref2, claimed_new_step, _new_attempt = claimed_new
    assert claimed_new_step.step_key == "current"
    assert order_ref2.status == "running"

    pending = await find_active_plan_succeeded_steps(db_session)

    assert claimed_old_step.id not in pending, (
        "a succeeded step from a superseded plan must not be re-selected for "
        "verification — its stale plan's own 'unfinished' siblings would "
        "corrupt the currently active plan's order status"
    )
    # Sanity: the order itself must be untouched by the lookup (it's a pure
    # read) and the actually-running step from the active plan is correctly
    # not "succeeded" yet, so it's not in the result either.
    assert order_ref2.status == "running"
    assert claimed_new_step.id not in pending


@pytest.mark.asyncio
async def test_unstick_ready_orders_recovers_order_whose_only_step_already_succeeded(
    db_session,
):
    """Ф4 (AGENT_AUTONOMY_ROADMAP.md): found live on the Ф4 pilot — a
    succeeded step that was the last claimable one in its plan leaves the
    order "ready" with nothing left for claim_ready_step to pick up (the
    only thing that would otherwise flip it back to "running"), so the
    succeeded step never gets verified and the order sits in "ready"
    forever. See unstick_ready_orders_with_stalled_active_plan's docstring.
    """
    order = await create_work_order(
        db_session, owner_key="tester", objective="Single step succeeds, nothing else to claim"
    )
    await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": "x"}
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    order_ref, step, attempt = claimed
    await complete_attempt(
        db_session, order=order_ref, step=step, attempt=attempt,
        output={"text": "ok"}, actor="w1",
    )
    await db_session.flush()
    assert order_ref.status == "running"  # complete_attempt itself never touches order.status

    # Simulate the order having been left at "ready" (e.g. by a race that
    # flips it back without anything claiming further work) — the exact
    # mechanism observed live doesn't matter here, only the resulting state:
    # a "ready" order whose active plan has one succeeded step and nothing
    # else in flight.
    order_ref.status = "ready"
    await db_session.flush()

    recovered = await unstick_ready_orders_with_stalled_active_plan(db_session)

    assert recovered == 1
    assert order_ref.status == "running"


@pytest.mark.asyncio
async def test_unstick_ready_orders_leaves_order_with_real_pending_work_untouched(db_session):
    """The narrow scope of the fix: an order resting in "ready" with a
    genuinely unclaimed sibling step must not be touched — only a plan with
    *nothing* left in any in-flight state qualifies."""
    order = await create_work_order(
        db_session, owner_key="tester", objective="One step done, one still unclaimed"
    )
    await create_work_plan(
        db_session,
        order,
        steps=[
            {
                "step_key": "done",
                "title": "done",
                "kind": "agent_turn",
                "input": {"prompt": "a"},
                "depends_on": [],
                "success_predicate": {"type": "no_error_and_nonempty_output"},
                "max_attempts": 1,
            },
            {
                "step_key": "still_pending",
                "title": "still pending",
                "kind": "agent_turn",
                "input": {"prompt": "b"},
                "depends_on": [],
                "success_predicate": {"type": "no_error_and_nonempty_output"},
                "max_attempts": 1,
            },
        ],
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    order_ref, step, attempt = claimed
    await complete_attempt(
        db_session, order=order_ref, step=step, attempt=attempt,
        output={"text": "ok"}, actor="w1",
    )
    await promote_ready_dependents(db_session, order=order_ref, plan_id=step.plan_id, actor="scheduler")
    await db_session.flush()
    assert order_ref.status == "ready"  # the other step ("still_pending") is genuinely unclaimed

    recovered = await unstick_ready_orders_with_stalled_active_plan(db_session)

    assert recovered == 0
    assert order_ref.status == "ready"


@pytest.mark.asyncio
async def test_enforce_budgets_blocks_order_that_overspent_cost_usd(db_session):
    """Ф1.C: max_cost_usd is symmetric with token_budget — same mechanism,
    summed over WorkStepAttempt.cost_usd instead of tokens_used."""
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Дорогой облачный вызов, который надо остановить по бюджету",
        budgets={"max_cost_usd": 1.0},
    )
    await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": order.objective}
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={"text": "готово", "cost_usd": 1.5},
        actor="worker",
    )
    await db_session.flush()
    assert attempt.cost_usd == 1.5
    assert claimed_order.status == "running"  # not blocked until enforce_budgets runs

    blocked = await enforce_budgets(db_session)

    assert blocked == 1
    assert claimed_order.status == "blocked"
    assert claimed_order.blocker["code"] == "cost_budget_exceeded"
    assert claimed_order.blocker["cost_spent_usd"] == 1.5


@pytest.mark.asyncio
async def test_enforce_budgets_blocks_order_that_ran_past_wall_clock_budget(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Долгий exploratory-ход с ограничением по времени",
        budgets={"max_wall_clock_seconds": 60},
    )
    await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": order.objective}
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, _claimed_step, _attempt = claimed
    assert claimed_order.started_at is not None  # set by claim_ready_step's running transition
    claimed_order.started_at = datetime.now(UTC) - timedelta(seconds=120)
    await db_session.flush()

    blocked = await enforce_budgets(db_session)

    assert blocked == 1
    assert claimed_order.status == "blocked"
    assert claimed_order.blocker["code"] == "wall_clock_budget_exceeded"
    assert claimed_order.blocker["max_wall_clock_seconds"] == 60


@pytest.mark.asyncio
async def test_enforce_budgets_ignores_order_within_wall_clock_budget(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Ещё не превысил лимит времени",
        budgets={"max_wall_clock_seconds": 3600},
    )
    await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": order.objective}
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, _claimed_step, _attempt = claimed

    blocked = await enforce_budgets(db_session)

    assert blocked == 0
    assert claimed_order.status == "running"


@pytest.mark.asyncio
async def test_enforce_budgets_blocks_order_that_exceeded_max_tool_calls(db_session):
    """Ф1.C: max_tool_calls counts every WorkToolCall row for the order
    regardless of status — the ceiling is on calls made, not on how many
    succeeded. Three independent steps (an exploratory plan's shape: several
    parallel discovery calls, not a dependency chain), each claimed for a
    real WorkStepAttempt — WorkToolCall.attempt_id is a real FK, not a value
    to fake with a random uuid."""
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Слишком много попыток веб-поиска для exploratory-хода",
        budgets={"max_tool_calls": 2},
    )
    _plan, steps = await create_work_plan(
        db_session,
        order,
        steps=[
            {
                "step_key": f"discover-{i}",
                "title": "Discover",
                "kind": "capability",
                "capability": "web_discover",
                "action": "search",
                "input": {},
            }
            for i in range(3)
        ],
    )
    for i, step in enumerate(steps):
        claimed = await claim_ready_step(db_session, worker_id=f"w{i}", work_order_id=order.id)
        assert claimed is not None
        _claimed_order, claimed_step, attempt = claimed
        db_session.add(
            WorkToolCall(
                work_order_id=order.id,
                step_id=claimed_step.id,
                attempt_id=attempt.id,
                call_no=1,
                executor="capability",
                capability="web_discover",
                action="search",
                arguments={},
                resolved_from={},
                risk_level="low",
                status="succeeded",
                action_digest=f"{i:064x}",
                idempotency_key=f"tool-call-budget-test:{i}",
            )
        )
    await db_session.flush()

    blocked = await enforce_budgets(db_session)

    assert blocked == 1
    assert order.status == "blocked"
    assert order.blocker["code"] == "tool_call_budget_exceeded"
    assert order.blocker["tool_calls_made"] == 3


@pytest.mark.asyncio
async def test_enforce_budgets_first_exceeded_budget_wins_the_blocker_reason(db_session):
    """Token budget is checked first — an order that blows both should report
    the token reason, not silently pick whichever query ran last."""
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Превышает и токены, и время",
        budgets={"token_budget": 10, "max_wall_clock_seconds": 60},
    )
    await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": order.objective}
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    claimed_order.started_at = datetime.now(UTC) - timedelta(seconds=120)
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={"text": "готово", "tokens_used": {"input_tokens": 50, "output_tokens": 50}},
        actor="worker",
    )
    await db_session.flush()

    blocked = await enforce_budgets(db_session)

    assert blocked == 1
    assert claimed_order.blocker["code"] == "token_budget_exceeded"


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
