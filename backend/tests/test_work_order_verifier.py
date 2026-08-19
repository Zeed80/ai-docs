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
    claimed = await claim_ready_step(
        db_session, worker_id="worker-1", work_order_id=order.id
    )
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
    passed = await verify_nonempty_result(
        db_session, order=claimed_order, step=claimed_step
    )
    await db_session.flush()

    assert passed is True
    assert claimed_order.status == "completed"
    assert claimed_step.state == "succeeded"
    assert claimed_order.completed_at is not None
    criterion = (
        await db_session.execute(
            select(WorkAcceptanceCriterion).where(
                WorkAcceptanceCriterion.work_order_id == order.id
            )
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
    claimed = await claim_ready_step(
        db_session, worker_id="worker-1", work_order_id=order.id
    )
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

    passed = await verify_nonempty_result(
        db_session, order=claimed_order, step=claimed_step
    )

    assert passed is False
    assert claimed_order.status == "blocked"
    assert claimed_order.blocker["code"] == "independent_verification_required"
    criterion = (
        await db_session.execute(
            select(WorkAcceptanceCriterion).where(
                WorkAcceptanceCriterion.work_order_id == order.id
            )
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
