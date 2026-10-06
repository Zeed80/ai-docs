"""E23: state of a superseded plan revision never acts on the current one.

Restart and concurrent planners are covered by the authority-digest tests in
test_work_budget_planner.py; the replan limit by test_work_order_replanning.py
and test_work_order_verifier.py.
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.jwt import _DEV_USER
from app.db.models import (
    Approval,
    ApprovalActionType,
    ApprovalStatus,
    WorkAcceptanceCriterion,
    WorkEvent,
    WorkOrder,
    WorkStep,
)
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import (
    WorkStateError,
    apply_approval_decision,
    claim_ready_step,
    complete_attempt,
    create_work_order,
    create_work_plan,
    fail_attempt,
    record_verifier_verdict,
    transition_step,
    transition_work_order,
    utcnow,
)
from app.domain.work_planning import _read_planner_snapshot
from app.tasks.work_orders import _read_verifier_snapshot, verify_completed_step

_SEMANTIC = [
    {
        "criterion_key": "answers_objective",
        "description": "Ответ отвечает на цель",
        "kind": "semantic",
        "predicate": {"type": "semantic"},
        "required": True,
    }
]


def _step(key, **extra):
    return {
        "step_key": key,
        "title": key,
        "kind": "capability",
        "capability": "invoices",
        "action": "list",
        "max_attempts": 1,
        **extra,
    }


async def _order(db, *, owner=_DEV_USER.sub, steps=("a",), **kwargs):
    order = await create_work_order(
        db, owner_key=owner, objective="Собрать счета", budgets={"max_replans": 3}, **kwargs
    )
    await initialize_budget_ledger(db, order.id)
    await create_work_plan(db, order, steps=[_step(key) for key in steps], actor="test")
    return order


async def _succeed(db, order, output=None):
    _order_row, step, attempt = await claim_ready_step(db, worker_id="w", work_order_id=order.id)
    await complete_attempt(
        db,
        order=order,
        step=step,
        attempt=attempt,
        output=output or {"text": f"результат {step.step_key}"},
        actor="w",
    )
    return step


@pytest.mark.asyncio
async def test_replan_waits_for_a_sibling_step_still_executing(db_session):
    order = await _order(db_session, steps=("a", "b"))
    _o, step_a, attempt_a = await claim_ready_step(
        db_session, worker_id="w1", work_order_id=order.id
    )
    _o, step_b, attempt_b = await claim_ready_step(
        db_session, worker_id="w2", work_order_id=order.id
    )

    await fail_attempt(
        db_session,
        order=order,
        step=step_a,
        attempt=attempt_a,
        error={"code": "boom"},
        retryable=False,
        actor="w1",
    )
    assert order.status == "replanning"
    # b may have performed its effect; the next revision is not planned blind.
    assert await _read_planner_snapshot(db_session, order.id, lock_order=False) is None

    await complete_attempt(
        db_session,
        order=order,
        step=step_b,
        attempt=attempt_b,
        output={"result": {"total": 2}},
        actor="w2",
    )
    snapshot = await _read_planner_snapshot(db_session, order.id, lock_order=False)
    assert [item["step_key"] for item in snapshot.completed_context] == ["b"]


@pytest.mark.asyncio
async def test_a_step_failing_during_replanning_does_not_restart_the_old_plan(db_session):
    order = await _order(db_session, steps=("a", "b"))
    _o, step_a, attempt_a = await claim_ready_step(
        db_session, worker_id="w1", work_order_id=order.id
    )
    _o, step_b, attempt_b = await claim_ready_step(
        db_session, worker_id="w2", work_order_id=order.id
    )
    await fail_attempt(
        db_session,
        order=order,
        step=step_a,
        attempt=attempt_a,
        error={"code": "boom"},
        retryable=False,
        actor="w1",
    )
    blocker = order.blocker

    step_b.max_attempts = 3
    await fail_attempt(
        db_session,
        order=order,
        step=step_b,
        attempt=attempt_b,
        error={"code": "timeout"},
        retryable=True,
        actor="w2",
    )

    assert order.status == "replanning"
    assert order.blocker == blocker
    assert step_b.state == "failed"
    settled = await db_session.scalar(
        select(func.count())
        .select_from(WorkEvent)
        .where(
            WorkEvent.work_order_id == order.id,
            WorkEvent.event_type == "step.settled_during_replanning",
        )
    )
    assert settled == 1


@pytest.mark.asyncio
async def test_superseded_steps_are_canceled_and_their_approval_expires(db_session):
    order = await _order(db_session, steps=("send",))
    _o, old_step, attempt = await claim_ready_step(
        db_session, worker_id="w", work_order_id=order.id
    )
    old_approval = Approval(
        action_type=ApprovalActionType.agent_tool_call,
        entity_type="work_order",
        entity_id=order.id,
        requested_by=order.owner_key,
        context={"work_order_id": str(order.id), "step_id": str(old_step.id)},
    )
    db_session.add(old_approval)
    attempt.status = "waiting_approval"
    await transition_step(db_session, old_step, "waiting_approval", actor="policy")
    await transition_work_order(db_session, order, "waiting_approval", actor="policy")
    await db_session.flush()

    # The owner revises the order while it waits; the planner writes revision 2.
    await transition_work_order(db_session, order, "replanning", actor="owner")
    await create_work_plan(db_session, order, steps=[_step("send_v2")], actor="planner")
    await db_session.refresh(old_approval)
    assert old_step.state == "canceled"
    assert old_approval.status == ApprovalStatus.expired

    _o, new_step, _attempt = await claim_ready_step(
        db_session, worker_id="w", work_order_id=order.id
    )
    await transition_step(db_session, new_step, "waiting_approval", actor="policy")
    await transition_work_order(db_session, order, "waiting_approval", actor="policy")

    with pytest.raises(WorkStateError):
        await apply_approval_decision(
            db_session,
            work_order_id=order.id,
            step_id=old_step.id,
            approval_id=old_approval.id,
            approved=True,
            actor="manager",
        )
    assert order.status == "waiting_approval"
    assert new_step.state == "waiting_approval"


@pytest.mark.asyncio
async def test_late_verification_of_a_superseded_step_is_ignored(test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await _order(db, steps=("a",))
        old_step = await _succeed(db, order)
        # A verification run sent revision 1 to replanning; revision 2 runs.
        await transition_work_order(db, order, "replanning", actor="verifier")
        await create_work_plan(db, order, steps=[_step("a2")], actor="planner")
        await claim_ready_step(db, worker_id="w", work_order_id=order.id)
        order_id, old_step_id = order.id, old_step.id
        await db.commit()

    # A duplicate verify task for revision 1's last step arrives late.
    assert await verify_completed_step(old_step_id, session_factory=factory) is False

    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        assert order.status == "running"
        assert order.plan_revision == 2


@pytest.mark.asyncio
async def test_a_verdict_only_lands_on_a_result_waiting_for_it(db_session):
    order = await _order(db_session, owner="someone-else", acceptance_criteria=_SEMANTIC)
    criterion = await db_session.scalar(
        select(WorkAcceptanceCriterion).where(WorkAcceptanceCriterion.work_order_id == order.id)
    )
    await claim_ready_step(db_session, worker_id="w", work_order_id=order.id)
    assert order.status == "running"

    # A rejection judged earlier output must not send the running revision away.
    with pytest.raises(WorkStateError):
        await record_verifier_verdict(
            db_session,
            order=order,
            criterion=criterion,
            ok=False,
            reason="старый результат",
            evidence_payload={},
            actor="manager",
        )
    assert order.status == "running"
    assert criterion.status == "pending"


@pytest.mark.asyncio
async def test_verdict_api_refuses_another_revision(client, db_session):
    order = await _order(db_session, owner="someone-else", acceptance_criteria=_SEMANTIC)
    await _succeed(db_session, order)
    await transition_work_order(db_session, order, "verifying", actor="verifier")
    order.blocker = {"code": "independent_verification_required", "criteria": ["answers_objective"]}
    await transition_work_order(db_session, order, "blocked", actor="verifier")
    criterion = await db_session.scalar(
        select(WorkAcceptanceCriterion).where(WorkAcceptanceCriterion.work_order_id == order.id)
    )
    await db_session.flush()
    url = f"/api/work-orders/{order.id}/criteria/{criterion.id}/verdict"

    stale = await client.post(url, json={"ok": True, "reason": "ок", "plan_revision": 0})
    assert stale.status_code == 409
    current = await client.post(url, json={"ok": True, "reason": "ок", "plan_revision": 1})
    assert current.status_code == 200, current.text
    assert current.json()["completed"] is True


@pytest.mark.asyncio
async def test_a_repeated_instruction_request_is_applied_once(client, db_session):
    order = await _order(db_session)
    order.blocker = {"code": "step_failed"}
    await transition_work_order(db_session, order, "blocked", actor="test")
    await db_session.flush()
    url = f"/api/work-orders/{order.id}/instructions"
    body = {"instruction": "Возьми только сентябрь", "request_id": "draft-1"}

    first = await client.post(url, json=body)
    second = await client.post(url, json=body)

    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "planning"
    await db_session.refresh(order)
    assert [item["text"] for item in order.metadata_["instructions"]] == ["Возьми только сентябрь"]
    added = await db_session.scalar(
        select(func.count())
        .select_from(WorkEvent)
        .where(
            WorkEvent.work_order_id == order.id,
            WorkEvent.event_type == "work.instruction_added",
        )
    )
    assert added == 1


@pytest.mark.asyncio
async def test_verifier_judges_the_revised_goal_with_reused_results(db_session):
    order = await _order(
        db_session, owner="someone-else", steps=("fetch", "draft"), acceptance_criteria=_SEMANTIC
    )
    await _succeed(db_session, order, {"result": {"total": 7}})
    await _succeed(db_session, order, {"text": "черновик"})
    order.metadata_ = {
        "instructions": [{"text": "Только счета за сентябрь", "actor": "someone-else"}]
    }
    # Revision 2 keeps the fetched data and redoes only the draft.
    await transition_work_order(db_session, order, "replanning", actor="owner")
    await create_work_plan(db_session, order, steps=[_step("draft")], actor="planner")
    await _succeed(db_session, order, {"text": "итог за сентябрь"})
    await transition_work_order(db_session, order, "verifying", actor="verifier")
    order.blocker = {"code": "independent_verification_required", "criteria": ["answers_objective"]}
    await transition_work_order(db_session, order, "blocked", actor="verifier")
    await db_session.flush()

    snapshot = await _read_verifier_snapshot(db_session, order.id, lock_order=False)

    assert snapshot.evidence["owner_instructions"] == ["Только счета за сентябрь"]
    assert [o["step"] for o in snapshot.evidence["outputs"]] == ["draft"]
    assert snapshot.evidence["outputs"][0]["output"]["text"] == "итог за сентябрь"
    assert [o["step"] for o in snapshot.evidence["outputs_from_earlier_revisions"]] == ["fetch"]


@pytest.mark.asyncio
async def test_a_new_revision_reuses_a_completed_child_order(test_engine, monkeypatch):
    import app.db.session as db_session_module
    from app.tasks.work_orders import _execute_decompose

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_session_module, "_get_session_factory", lambda: factory)
    spec = {"children": [{"objective": "Поставщик А: собрать счета"}]}
    async with factory() as db:
        order = await create_work_order(db, owner_key="tester", objective="Отчёт по поставщику")
        await initialize_budget_ledger(db, order.id)
        await create_work_plan(
            db, order, steps=[{"step_key": "fan", "kind": "decompose", "input": spec}]
        )
        order_id = order.id
        await db.commit()

    first = await _execute_decompose(order_id, spec)
    async with factory() as db:
        child = await db.get(WorkOrder, first["child_order_ids"][0])
        child.status = "completed"
        child.completed_at = utcnow()
        parent = await db.get(WorkOrder, order_id)
        parent.status = "replanning"
        await create_work_plan(
            db, parent, steps=[{"step_key": "fan", "kind": "decompose", "input": spec}]
        )
        await db.commit()

    again = await _execute_decompose(order_id, spec)

    assert again["child_order_ids"] == first["child_order_ids"]
    async with factory() as db:
        children = await db.scalar(
            select(func.count()).select_from(WorkOrder).where(WorkOrder.parent_id == order_id)
        )
        assert children == 1
        steps = await db.scalar(
            select(func.count()).select_from(WorkStep).where(WorkStep.work_order_id == order_id)
        )
        assert steps == 2
