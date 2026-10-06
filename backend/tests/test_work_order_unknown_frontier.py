"""E23: an unknown-outcome effect blocks replanning until the owner reconciles it."""

import pytest

from app.auth.jwt import _DEV_USER
from app.db.models import WorkToolCall
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import (
    _replan_or_block,
    claim_ready_step,
    create_work_order,
    create_work_plan,
)
from app.domain.work_planning import _read_planner_snapshot


async def _order_with_unknown_send(db):
    order = await create_work_order(db, owner_key=_DEV_USER.sub, objective="Отправить письмо")
    await initialize_budget_ledger(db, order.id)
    await create_work_plan(
        db,
        order,
        steps=[
            {
                "step_key": "send",
                "title": "Отправить",
                "kind": "capability",
                "capability": "email",
                "action": "send",
                "input": {"draft_id": "d"},
            }
        ],
        actor="test",
    )
    _order, step, attempt = await claim_ready_step(db, worker_id="w", work_order_id=order.id)
    call = WorkToolCall(
        work_order_id=order.id,
        step_id=step.id,
        attempt_id=attempt.id,
        call_no=1,
        executor="capability",
        capability="email",
        action="send",
        arguments={"draft_id": "d"},
        status="outcome_unknown",
        action_digest="0" * 64,
        idempotency_key="k",
    )
    db.add(call)
    # As stop_attempt_for_nonterminal_tool_result leaves it: the step is
    # settled, only the effect of its call is unknown.
    step.state = "failed"
    step.lease_owner = None
    attempt.status = "failed"
    await db.flush()
    return order, step, call


@pytest.mark.asyncio
async def test_automatic_replan_is_blocked_by_an_unknown_effect(db_session):
    order, _step, call = await _order_with_unknown_send(db_session)
    order.blocker = {"code": "step_failed"}

    await _replan_or_block(db_session, order, actor="test", guarded=False)

    assert order.status == "blocked"
    assert order.blocker["code"] == "unresolved_effect_requires_reconciliation"
    assert order.blocker["tool_call_ids"] == [str(call.id)]
    assert order.blocker["cause"] == {"code": "step_failed"}


@pytest.mark.asyncio
async def test_owner_reconciles_before_an_instruction_can_replan(client, db_session):
    order, step, call = await _order_with_unknown_send(db_session)
    await _replan_or_block(db_session, order, actor="test", guarded=False)
    await db_session.flush()

    refused = await client.post(
        f"/api/work-orders/{order.id}/instructions", json={"instruction": "Попробуй снова"}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["error_code"] == "unresolved_effect_requires_reconciliation"

    done = await client.post(
        f"/api/work-orders/{order.id}/tool-calls/{call.id}/reconcile",
        json={"outcome": "happened", "note": "Письмо пришло адресату"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "reconciled_happened"
    again = await client.post(
        f"/api/work-orders/{order.id}/tool-calls/{call.id}/reconcile",
        json={"outcome": "not_happened"},
    )
    assert again.status_code == 409

    allowed = await client.post(
        f"/api/work-orders/{order.id}/instructions", json={"instruction": "Продолжай"}
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "planning"

    # The confirmed send is completed work for the planner, not something to redo.
    snapshot = await _read_planner_snapshot(db_session, order.id, lock_order=False)
    confirmed = [c for c in snapshot.completed_context if c["step_key"] == step.step_key]
    assert confirmed and confirmed[0]["output"]["owner_confirmed_effect"] is True
