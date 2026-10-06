"""E21.3b: decomposed children run on the parent's ledger; plans stay executable."""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import WorkEvent, WorkOrder, WorkPlan
from app.domain.work_budget_ledger import BudgetBindingConflict, initialize_budget_ledger
from app.domain.work_orders import create_work_order
from app.domain.work_planning import _MAX_CONSECUTIVE_PLANNER_FALLBACKS, plan_work_order_detached
from app.tasks.work_orders import _execute_decompose
from tests.test_work_budget_planner import (
    _install_http,
    _install_model,
    _planning_case,
    _reservations,
    _response,
)


def _plan_response(steps):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            content = json.dumps(
                {"assumptions": [], "steps": steps, "verification_plan": {"mode": "x"}}
            )
            return {"message": {"content": content}}

    return Response()


async def _parent_with_ledger(factory):
    async with factory() as db:
        parent = await create_work_order(db, owner_key="planner-owner", objective="Parent")
        await initialize_budget_ledger(db, parent.id)
        await db.commit()
        return parent.id, parent.budget_ledger_id


@pytest.mark.asyncio
async def test_child_is_planned_and_charged_on_the_parent_ledger(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    parent_id, ledger_id = await _parent_with_ledger(factory)

    output = await _execute_decompose(parent_id, {"children": [{"objective": "Child A"}]})
    [child_id] = output["child_order_ids"]

    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [_response()], calls)
    assert await plan_work_order_detached(child_id, session_factory=factory)

    async with factory() as db:
        child = await db.get(WorkOrder, child_id)
        assert child.status == "ready"
        assert child.budget_ledger_id == ledger_id
    charged = [row for row in await _reservations(factory, child_id) if row.reserved_units == 1]
    assert len(calls) == len(charged) == 1
    assert charged[0].ledger_id == ledger_id and charged[0].state == "charged"


@pytest.mark.asyncio
async def test_child_without_a_parent_ledger_is_refused(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    async with factory() as db:
        parent = await create_work_order(db, owner_key="planner-owner", objective="Legacy parent")
        await db.commit()

    with pytest.raises(BudgetBindingConflict):
        await _execute_decompose(parent.id, {"children": [{"objective": "Child"}]})
    async with factory() as db:
        children = await db.scalars(select(WorkOrder).where(WorkOrder.parent_id == parent.id))
        assert list(children) == []  # rolled back together


@pytest.mark.asyncio
async def test_agent_turn_plan_is_rejected_with_a_reason_and_no_dead_step(test_engine, monkeypatch):
    factory, order_id = await _planning_case(test_engine)
    _install_model(monkeypatch)
    calls = []
    steps = [{"step_key": "sum", "title": "Summary", "kind": "agent_turn", "input": {}}]
    _install_http(monkeypatch, [_plan_response(steps)], calls)

    assert not await plan_work_order_detached(order_id, session_factory=factory)

    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        assert order.status == "planning"
        assert "agent_turn is not executable" in order.metadata_["last_planner_error"]
        assert (
            list(await db.scalars(select(WorkPlan).where(WorkPlan.work_order_id == order_id))) == []
        )
        events = list(
            await db.scalars(
                select(WorkEvent.event_type).where(WorkEvent.work_order_id == order_id)
            )
        )
        assert "plan.planner_failed" in events


@pytest.mark.asyncio
async def test_planner_failure_streak_blocks_the_order(test_engine, monkeypatch):
    factory, order_id = await _planning_case(test_engine)
    _install_model(monkeypatch)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        order.metadata_ = {"planner_fallback_streak": _MAX_CONSECUTIVE_PLANNER_FALLBACKS - 1}
        await db.commit()
    _install_http(monkeypatch, [_response(valid=False)] * 3, [])

    assert not await plan_work_order_detached(order_id, session_factory=factory)

    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        assert order.status == "blocked"
        assert order.blocker["code"] == "planner_schema_failure_streak"
        assert order.blocker["streak"] == _MAX_CONSECUTIVE_PLANNER_FALLBACKS
