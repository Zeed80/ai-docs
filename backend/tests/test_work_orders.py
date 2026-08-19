"""Durable work-order CRUD/API contracts, learning materialisation, DAG/dataflow.

Б18: split from one 555-line file into thematic files — this one keeps
CRUD/API-level contracts and the domain flows that don't fit the other
buckets (learning provenance, approval binding, DAG dependents, dataflow
resolution, capability-plan validation). See:
  - test_work_order_lease.py      — lease recovery, budget housekeeping,
                                     concurrent-claim race guard
  - test_work_order_verifier.py   — independent verification, acceptance
                                     criteria
  - test_work_order_replanning.py — bounded replanning after terminal
                                     failure
  - test_computer_use_grants.py   — ComputerUseGrant broker enforcement
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import pytest
from sqlalchemy import select

from app.db.models import MemoryFact, RecipeSkill, WorkLearning, WorkToolCall
from app.domain.work_learning import process_work_learning
from app.domain.work_orders import (
    apply_approval_decision,
    claim_ready_step,
    complete_attempt,
    create_single_step_plan,
    create_work_order,
    create_work_plan,
    promote_ready_dependents,
    transition_step,
    transition_work_order,
    verify_nonempty_result,
)
from app.domain.work_planning import (
    PlannedStep,
    PlannedWork,
    resolve_step_input,
    validate_capability_plan,
)


@pytest.mark.asyncio
async def test_completed_work_order_materializes_provenance_memory_and_recipe(test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        recipe = RecipeSkill(
            name="learned-test",
            role="autonomous_worker",
            trigger_examples=["Проверить обучение"],
            steps=[],
            status="draft",
        )
        db.add(recipe)
        await db.flush()
        recipe_id = recipe.id
        order = await create_work_order(
            db,
            owner_key="learning-owner",
            objective="Проверить обучение из завершённого поручения",
            source="test",
        )
        _plan, step = await create_single_step_plan(
            db,
            order,
            kind="capability",
            title="Read data",
            input_data={"query": "status"},
            capability="workspace",
            action="read",
        )
        claimed = await claim_ready_step(db, worker_id="learning-worker", work_order_id=order.id)
        assert claimed is not None
        claimed_order, claimed_step, attempt = claimed
        db.add(
            WorkToolCall(
                work_order_id=order.id,
                step_id=step.id,
                attempt_id=attempt.id,
                call_no=1,
                executor="capability",
                capability="workspace",
                action="read",
                arguments={"query": "status"},
                resolved_from={},
                risk_level="low",
                status="succeeded",
                action_digest="a" * 64,
                idempotency_key=f"learning-test:{order.id}",
                output={"text": "Состояние получено"},
            )
        )
        await complete_attempt(
            db,
            order=claimed_order,
            step=claimed_step,
            attempt=attempt,
            output={"text": "Состояние получено"},
            actor="learning-worker",
        )
        assert await verify_nonempty_result(db, order=claimed_order, step=claimed_step)
        order_id = order.id
        await db.commit()

    captured: dict = {}

    async def fake_recipe_recorder(**kwargs):
        captured.update(kwargs)
        return True, str(recipe_id)

    assert await process_work_learning(
        order_id,
        session_factory=factory,
        recipe_recorder=fake_recipe_recorder,
    )

    async with factory() as db:
        learning = (
            await db.execute(
                select(WorkLearning).where(WorkLearning.work_order_id == order_id)
            )
        ).scalar_one()
        fact = await db.get(MemoryFact, learning.memory_fact_id)
        assert learning.status == "recorded"
        assert learning.recipe_skill_id == recipe_id
        assert learning.provenance["work_order_id"] == str(order_id)
        assert learning.provenance["tool_calls"][0]["digest"] == "a" * 64
        assert fact is not None
        assert fact.kind == "work_order_lesson"
        assert fact.scope == "owner:learning-owner"
        assert fact.confidence == 1.0
        assert captured["steps"][0]["capability"] == "workspace"
        assert captured["session_id"] == str(order_id)


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
