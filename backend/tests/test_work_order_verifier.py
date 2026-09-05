"""Independent verification and acceptance-criteria gating — Celery SUCCESS is
never treated as proof of a business result; completion requires passed
criteria with evidence.

Split out of test_work_orders.py (Б18) — see that file's docstring for the
full split map.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import WorkAcceptanceCriterion, WorkEvent
from app.domain.work_orders import (
    WorkStateError,
    claim_ready_step,
    complete_attempt,
    create_single_step_plan,
    create_work_order,
    record_verifier_verdict,
    transition_work_order,
    verify_nonempty_result,
)

# ── Ф4-re (AGENT_AUTONOMY_ROADMAP.md): a failed acceptance criterion must ──
# get the same bounded-replan chance as a step-execution failure, not go
# straight to "blocked" regardless of budget. Found live on the persistence
# re-verification pilot: a required criterion genuinely failing (not merely
# "unresolved, pending the independent verifier") always terminated the
# order on the very first attempt, with zero regard for max_replans — only
# fail_attempt/reclaim_expired_leases (step-level failures) ever consumed
# that budget.


@pytest.mark.asyncio
async def test_deterministic_criterion_failure_replans_within_budget(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Слишком короткий результат должен дать ещё одну попытку",
        budgets={"max_replans": 3},
        acceptance_criteria=[
            {
                "criterion_key": "long_enough",
                "description": "Результат не короче 200 символов",
                "kind": "artifact",
                "predicate": {"type": "min_length", "value": 200},
                "required": True,
            }
        ],
    )
    await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": "x"}
    )
    claimed = await claim_ready_step(db_session, worker_id="worker-1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={"text": "слишком коротко"},
        actor="worker-1",
    )

    passed = await verify_nonempty_result(db_session, order=claimed_order, step=claimed_step)

    assert passed is False
    assert claimed_order.status == "replanning", (
        "a genuinely failed (not unresolved) required criterion must consume the "
        "replan budget, not block on the very first attempt"
    )
    assert claimed_order.blocker["code"] == "verification_failed"
    assert claimed_order.blocker["criteria"] == ["long_enough"]


@pytest.mark.asyncio
async def test_deterministic_criterion_failure_blocks_once_replan_budget_exhausted(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Бюджет реплана исчерпан — терминальный blocked",
        budgets={"max_replans": 1},
        acceptance_criteria=[
            {
                "criterion_key": "long_enough",
                "description": "Результат не короче 200 символов",
                "kind": "artifact",
                "predicate": {"type": "min_length", "value": 200},
                "required": True,
            }
        ],
    )
    await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": "x"}
    )
    order.plan_revision = 2  # already past max_replans=1
    claimed = await claim_ready_step(db_session, worker_id="worker-1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={"text": "слишком коротко"},
        actor="worker-1",
    )

    passed = await verify_nonempty_result(db_session, order=claimed_order, step=claimed_step)

    assert passed is False
    assert claimed_order.status == "blocked"


@pytest.mark.asyncio
async def test_semantic_verifier_rejection_replans_within_budget(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Независимый verifier отклонил результат — ещё одна попытка",
        budgets={"max_replans": 3},
        acceptance_criteria=[
            {
                "criterion_key": "semantic_quality",
                "description": "Результат соответствует задаче",
                "kind": "semantic",
                "predicate": {},
                "required": True,
            }
        ],
    )
    _plan, step = await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Execute",
        input_data={"prompt": order.objective},
    )
    claimed = await claim_ready_step(db_session, worker_id="worker-1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={"text": "Правдоподобный, но ещё не проверенный результат"},
        actor="worker-1",
    )
    passed = await verify_nonempty_result(db_session, order=claimed_order, step=claimed_step)
    assert passed is False
    assert claimed_order.status == "blocked"
    assert claimed_order.blocker["code"] == "independent_verification_required"
    criterion = (
        await db_session.execute(
            select(WorkAcceptanceCriterion).where(WorkAcceptanceCriterion.work_order_id == order.id)
        )
    ).scalar_one()

    completed = await record_verifier_verdict(
        db_session,
        order=claimed_order,
        criterion=criterion,
        ok=False,
        reason="Не подтверждено независимой проверкой",
        evidence_payload={"checklist": []},
        actor="independent-verifier",
    )

    assert completed is False
    assert claimed_order.status == "replanning", (
        "a rejected semantic criterion must consume the replan budget, not "
        "leave the order stuck on 'blocked' with no automatic recovery path"
    )
    assert claimed_order.blocker["code"] == "verification_failed"
    assert claimed_order.blocker["criterion"] == "semantic_quality"


@pytest.mark.asyncio
async def test_semantic_verifier_rejection_blocks_once_replan_budget_exhausted(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Бюджет реплана исчерпан на семантическом провале",
        budgets={"max_replans": 1},
        acceptance_criteria=[
            {
                "criterion_key": "semantic_quality",
                "description": "Результат соответствует задаче",
                "kind": "semantic",
                "predicate": {},
                "required": True,
            }
        ],
    )
    _plan, step = await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Execute",
        input_data={"prompt": order.objective},
    )
    claimed = await claim_ready_step(db_session, worker_id="worker-1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={"text": "Правдоподобный, но ещё не проверенный результат"},
        actor="worker-1",
    )
    await verify_nonempty_result(db_session, order=claimed_order, step=claimed_step)
    claimed_order.plan_revision = 2  # already past max_replans=1
    criterion = (
        await db_session.execute(
            select(WorkAcceptanceCriterion).where(WorkAcceptanceCriterion.work_order_id == order.id)
        )
    ).scalar_one()

    completed = await record_verifier_verdict(
        db_session,
        order=claimed_order,
        criterion=criterion,
        ok=False,
        reason="Не подтверждено независимой проверкой",
        evidence_payload={"checklist": []},
        actor="independent-verifier",
    )

    assert completed is False
    assert claimed_order.status == "blocked"


@pytest.mark.asyncio
async def test_rejected_semantic_criterion_is_reset_to_pending_on_the_next_attempt(
    db_session,
):
    """Ф4-re (AGENT_AUTONOMY_ROADMAP.md): found live on the persistence
    re-verification pilot — record_verifier_verdict leaves a rejected
    semantic criterion's status at "failed", and verify_semantic_criteria
    only ever looks at status=="pending" criteria. Without a reset, the
    independent verifier would never run again on any later replan attempt,
    no matter how much better the new evidence was — the order could
    replan forever but never reach a genuine verdict either way. This pins
    that verify_nonempty_result resets it back to "pending" once it
    determines (again) that independent judgment is needed."""
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Семантический критерий должен пересматриваться на каждой попытке",
        acceptance_criteria=[
            {
                "criterion_key": "semantic_quality",
                "description": "Результат соответствует задаче",
                "kind": "semantic",
                "predicate": {},
                "required": True,
            }
        ],
    )
    _plan, step = await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Execute",
        input_data={"prompt": order.objective},
    )
    claimed = await claim_ready_step(db_session, worker_id="worker-1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={"text": "Первая, слабая попытка"},
        actor="worker-1",
    )
    await verify_nonempty_result(db_session, order=claimed_order, step=claimed_step)
    criterion = (
        await db_session.execute(
            select(WorkAcceptanceCriterion).where(WorkAcceptanceCriterion.work_order_id == order.id)
        )
    ).scalar_one()
    # Independent verifier rejects it — status left "failed" by
    # record_verifier_verdict, same as it always has been.
    await record_verifier_verdict(
        db_session,
        order=claimed_order,
        criterion=criterion,
        ok=False,
        reason="Недостаточно доказательств",
        evidence_payload={},
        actor="independent-verifier",
    )
    assert criterion.status == "failed"
    # This fix's own side effect: a rejection with replan budget remaining
    # already sent the order to "replanning" (see the dedicated
    # test_semantic_verifier_rejection_replans_within_budget above) — walk
    # the FSM forward the way a real replan cycle would (new plan -> ready
    # -> claimed -> running) before the next verification pass.
    assert claimed_order.status == "replanning"
    await transition_work_order(db_session, claimed_order, "ready", actor="test")
    await transition_work_order(db_session, claimed_order, "running", actor="test")

    # A fresh replan produces a new, better step — verify_nonempty_result
    # runs again on its output.
    new_step = claimed_step  # same step row stands in for "a later attempt's output" here
    new_step.output = {"text": "Вторая, гораздо более подробная и обоснованная попытка"}
    await verify_nonempty_result(db_session, order=claimed_order, step=new_step)

    assert criterion.status == "pending", (
        "a rejected semantic criterion must be re-armed for judgment on the "
        "next attempt, not stay permanently 'failed' — otherwise "
        "verify_semantic_criteria's status=='pending' filter would silently "
        "skip it forever"
    )
    assert criterion.verdict is None


@pytest.mark.asyncio
async def test_verified_work_order_completes_with_evidence(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Подготовить проверяемый результат",
    )
    _plan, step = await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Execute",
        input_data={"prompt": order.objective},
    )
    claimed = await claim_ready_step(db_session, worker_id="worker-1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed

    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={"text": "Результат создан и проверен."},
        actor="worker-1",
    )
    passed = await verify_nonempty_result(db_session, order=claimed_order, step=claimed_step)
    await db_session.flush()

    assert passed is True
    assert claimed_order.status == "completed"
    assert claimed_step.state == "succeeded"
    assert claimed_order.completed_at is not None
    criterion = (
        await db_session.execute(
            select(WorkAcceptanceCriterion).where(WorkAcceptanceCriterion.work_order_id == order.id)
        )
    ).scalar_one()
    assert criterion.status == "passed"
    events = list(
        (
            await db_session.execute(
                select(WorkEvent)
                .where(WorkEvent.work_order_id == order.id)
                .order_by(WorkEvent.sequence)
            )
        ).scalars()
    )
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[-1].payload["to"] == "completed"
    assert step.id == claimed_step.id


@pytest.mark.asyncio
async def test_completion_is_blocked_until_required_criteria_pass(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Не разрешать ложный completed",
    )
    await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Execute",
        input_data={"prompt": order.objective},
    )
    await transition_work_order(db_session, order, "running", actor="test")
    await transition_work_order(db_session, order, "verifying", actor="test")

    with pytest.raises(WorkStateError, match="required criteria"):
        await transition_work_order(db_session, order, "completed", actor="test")
    assert order.status == "verifying"


@pytest.mark.asyncio
async def test_unknown_required_criterion_blocks_false_completion(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Не заявлять успех без независимой проверки",
        acceptance_criteria=[
            {
                "criterion_key": "semantic_quality",
                "description": "Результат соответствует задаче",
                "kind": "semantic",
                "predicate": {},
                "required": True,
            }
        ],
    )
    _plan, step = await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Execute",
        input_data={"prompt": order.objective},
    )
    claimed = await claim_ready_step(db_session, worker_id="worker-1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={"text": "Правдоподобный, но ещё не проверенный результат"},
        actor="worker-1",
    )

    passed = await verify_nonempty_result(db_session, order=claimed_order, step=claimed_step)

    assert passed is False
    assert claimed_order.status == "blocked"
    assert claimed_order.blocker["code"] == "independent_verification_required"
    criterion = (
        await db_session.execute(
            select(WorkAcceptanceCriterion).where(WorkAcceptanceCriterion.work_order_id == order.id)
        )
    ).scalar_one()

    completed = await record_verifier_verdict(
        db_session,
        order=claimed_order,
        criterion=criterion,
        ok=True,
        reason="Проверено независимым контуром",
        evidence_payload={"checklist": ["objective", "constraints"]},
        actor="independent-verifier",
    )

    assert completed is True
    assert claimed_order.status == "completed"
