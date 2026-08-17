"""Durable work-order state machine, recovery, verification, and API contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import WorkAcceptanceCriterion, WorkEvent, WorkPlan
from app.domain.work_planning import (
    PlannedStep,
    PlannedWork,
    resolve_step_input,
    validate_capability_plan,
)
from app.domain.work_orders import (
    WorkStateError,
    apply_approval_decision,
    claim_ready_step,
    complete_attempt,
    create_single_step_plan,
    create_work_order,
    create_work_plan,
    promote_ready_dependents,
    reclaim_expired_leases,
    record_verifier_verdict,
    transition_step,
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
async def test_approval_is_bound_to_step_and_resumes_execution(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Выполнить действие после согласования",
    )
    _plan, step = await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Execute",
        input_data={"prompt": order.objective},
    )
    await transition_step(db_session, step, "waiting_approval", actor="policy")
    await transition_work_order(db_session, order, "waiting_approval", actor="policy")
    approval_id = uuid.uuid4()

    await apply_approval_decision(
        db_session,
        work_order_id=order.id,
        step_id=step.id,
        approval_id=approval_id,
        approved=True,
        actor="manager",
        action_digest="abc123",
    )

    assert order.status == "ready"
    assert step.state == "ready"
    assert step.input_["approval"] == {
        "approval_id": str(approval_id),
        "action_digest": "abc123",
        "approved_by": "manager",
    }


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


@pytest.mark.asyncio
async def test_dag_plan_releases_only_satisfied_dependents(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Выполнить два зависимых шага",
    )
    plan, steps = await create_work_plan(
        db_session,
        order,
        steps=[
            {
                "step_key": "collect",
                "title": "Collect",
                "kind": "capability",
                "capability": "documents",
                "action": "list",
                "input": {},
            },
            {
                "step_key": "summarize",
                "title": "Summarize",
                "kind": "agent_turn",
                "input": {"prompt": "Summarize"},
                "depends_on": ["collect"],
            },
        ],
    )
    first, second = steps
    assert first.state == "ready"
    assert second.state == "pending"
    await transition_work_order(db_session, order, "running", actor="worker")
    await transition_step(db_session, first, "running", actor="worker")
    await transition_step(db_session, first, "succeeded", actor="worker")

    unfinished = await promote_ready_dependents(
        db_session, order=order, plan_id=plan.id
    )

    assert unfinished is True
    assert second.state == "ready"
    assert order.status == "ready"


@pytest.mark.asyncio
async def test_dataflow_resolves_only_succeeded_step_outputs(db_session):
    order = await create_work_order(db_session, owner_key="tester", objective="Dataflow")
    _plan, steps = await create_work_plan(
        db_session,
        order,
        steps=[
            {"step_key": "lookup", "title": "Lookup", "kind": "agent_turn", "input": {"prompt": "x"}},
            {
                "step_key": "consume",
                "title": "Consume",
                "kind": "capability",
                "capability": "documents",
                "action": "get",
                "input": {"document_id": "${steps.lookup.output.result.id}"},
                "depends_on": ["lookup"],
            },
        ],
    )
    lookup, consume = steps
    lookup.output = {"result": {"id": "doc-42"}}
    lookup.state = "succeeded"
    resolved, provenance = await resolve_step_input(db_session, consume)
    assert resolved == {"document_id": "doc-42"}
    assert provenance["/document_id"] == {"step_key": "lookup", "path": "result.id"}


def test_planner_rejects_unknown_capability_action():
    plan = PlannedWork(
        steps=[
            PlannedStep(
                step_key="bad",
                title="Bad",
                kind="capability",
                capability="documents",
                action="destroy_everything",
            )
        ]
    )
    with pytest.raises(ValueError, match="unknown action"):
        validate_capability_plan(plan)


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
    from app.domain.work_orders import fail_attempt

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


@pytest.mark.asyncio
async def test_work_order_api_create_plan_events_and_cancel(client):
    response = await client.post(
        "/api/work-orders",
        json={
            "objective": "Собрать отчёт",
            "description": "Использовать только данные проекта",
            "priority": 70,
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] == "ready"
    assert created["plan_revision"] == 1
    work_order_id = created["id"]

    plan = await client.get(f"/api/work-orders/{work_order_id}/plan")
    assert plan.status_code == 200, plan.text
    assert plan.json()["steps"][0]["state"] == "ready"
    assert plan.json()["steps"][0]["kind"] == "agent_turn"
    step_id = plan.json()["steps"][0]["id"]

    approval = await client.post(
        f"/api/work-orders/{work_order_id}/approvals",
        json={
            "step_id": step_id,
            "capability": "email",
            "action": "send",
            "arguments": {"to": "customer@example.test"},
            "reason": "Внешнее необратимое действие",
        },
    )
    assert approval.status_code == 201, approval.text
    assert approval.json()["status"] == "pending"
    assert len(approval.json()["action_digest"]) == 64

    waiting = await client.get(f"/api/work-orders/{work_order_id}")
    assert waiting.json()["status"] == "waiting_approval"

    events = await client.get(f"/api/work-orders/{work_order_id}/events")
    assert events.status_code == 200, events.text
    sequences = [event["sequence"] for event in events.json()]
    assert sequences == list(range(1, len(sequences) + 1))

    canceled = await client.post(f"/api/work-orders/{work_order_id}/cancel")
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "canceled"


@pytest.mark.asyncio
async def test_computer_use_broker_enforces_grant_and_audits_file_action(client, tmp_path):
    created = await client.post("/api/work-orders", json={"objective": "Write broker file"})
    assert created.status_code == 201
    order_id = created.json()["id"]
    target = tmp_path / "result.txt"
    denied = await client.post(
        "/api/computer-use/execute",
        json={"action": "file_write", "work_order_id": order_id, "target": str(target), "arguments": {"content": "verified"}},
    )
    assert denied.status_code == 423
    granted = await client.post(
        f"/api/work-orders/{order_id}/computer-grants",
        json={"actions": ["file_write", "file_read"], "allowed_roots": [str(tmp_path)], "max_actions": 2, "reason": "test"},
    )
    assert granted.status_code == 201, granted.text
    written = await client.post(
        "/api/computer-use/execute",
        json={"action": "file_write", "work_order_id": order_id, "target": str(target), "arguments": {"content": "verified"}},
    )
    assert written.status_code == 200, written.text
    assert written.json()["result"]["sha256"]
    read = await client.post(
        "/api/computer-use/execute",
        json={"action": "file_read", "work_order_id": order_id, "target": str(target)},
    )
    assert read.status_code == 200, read.text
    assert read.json()["result"]["content"] == "verified"
