"""E24: a durable tool effect commits only while its attempt is current.

The fence runs in before_commit of the recipient's own session, in the same
transaction as the effect; a rejected check leaves nothing written.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.auth import effect_fence as ef
from app.config import settings
from app.db.models import Party, WorkEffectReceipt, WorkOrder, WorkPlan, WorkStep
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import (
    cancel_work_order,
    claim_ready_step,
    create_work_order,
    create_work_plan,
    transition_work_order,
    utcnow,
)


def _step(key):
    return {"step_key": key, "title": key, "kind": "capability", "capability": "x", "action": "y"}


async def _claimed(factory):
    async with factory() as db:
        order = await create_work_order(db, owner_key="fence-owner", objective="Эффект")
        await initialize_budget_ledger(db, order.id)
        plan, _ = await create_work_plan(db, order, steps=[_step("write")], actor="test")
        _o, step, attempt = await claim_ready_step(db, worker_id="w1", work_order_id=order.id)
        await db.commit()
        return order.id, plan.id, step.id, attempt.id


def _token(order_id, plan_id, step_id, attempt_id, *, revision=1, key=None):
    return ef.new_effect_fence(
        work_order_id=order_id,
        step_id=step_id,
        attempt_id=attempt_id,
        plan_id=plan_id,
        plan_revision=revision,
        operation_key=key or f"tool:{attempt_id}:p1:post:{uuid.uuid4().hex[:12]}",
    )


async def _commit_effect(factory, token, inn):
    """What a recipient endpoint does: one write and a commit, fenced."""
    bound = ef._RequestFence(fence=ef.verify_effect_fence(token), method="POST", path="/api/x")
    reset = ef._request_fence.set(bound)
    try:
        async with factory() as db:
            ef.install_effect_fence(db)
            db.add(Party(name=f"Эффект {inn}", inn=inn))
            await db.commit()
    finally:
        ef._request_fence.reset(reset)


async def _effects(factory, inn):
    async with factory() as db:
        return await db.scalar(select(func.count()).select_from(Party).where(Party.inn == inn))


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def _inn():
    return str(uuid.uuid4().int)[:12]


@pytest.mark.asyncio
async def test_the_current_attempt_commits_its_effect_once_with_a_receipt(factory):
    ids = await _claimed(factory)
    token = _token(*ids, key="tool:once")
    inn = _inn()

    await _commit_effect(factory, token, inn)
    assert await _effects(factory, inn) == 1
    async with factory() as db:
        receipt = await db.scalar(
            select(WorkEffectReceipt).where(WorkEffectReceipt.operation_key == "tool:once")
        )
    assert receipt.attempt_id == ids[3]

    # The same token replayed: no second effect.
    with pytest.raises(ef.EffectFenceRejected, match="effect_already_recorded"):
        await _commit_effect(factory, token, _inn())


@pytest.mark.asyncio
async def test_a_stale_worker_after_lease_transfer_writes_nothing(factory):
    order_id, plan_id, step_id, attempt_id = await _claimed(factory)
    token = _token(order_id, plan_id, step_id, attempt_id)
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        step.lease_expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    # Another worker took the step over.
    from app.domain.work_orders import reclaim_expired_leases

    async with factory() as db:
        await reclaim_expired_leases(db)
        await db.commit()
    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w2", work_order_id=order_id)
        await db.commit()
    inn = _inn()

    with pytest.raises(ef.EffectFenceRejected, match="attempt_lease_lost"):
        await _commit_effect(factory, token, inn)
    assert await _effects(factory, inn) == 0
    # The new owner commits normally.
    if claimed is not None:
        _o, _s, new_attempt = claimed
        await _commit_effect(factory, _token(order_id, plan_id, step_id, new_attempt.id), inn)
        assert await _effects(factory, inn) == 1


@pytest.mark.asyncio
async def test_cancellation_between_preparation_and_send_writes_nothing(factory):
    ids = await _claimed(factory)
    token = _token(*ids)
    async with factory() as db:
        order = await db.get(WorkOrder, ids[0], with_for_update=True)
        await cancel_work_order(db, order, actor="owner")
        await db.commit()
    inn = _inn()

    with pytest.raises(ef.EffectFenceRejected, match="work_order_not_live"):
        await _commit_effect(factory, token, inn)
    assert await _effects(factory, inn) == 0


@pytest.mark.asyncio
async def test_a_superseded_plan_writes_nothing(factory):
    order_id, plan_id, step_id, attempt_id = await _claimed(factory)
    token = _token(order_id, plan_id, step_id, attempt_id)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id, with_for_update=True)
        plan = await db.get(WorkPlan, plan_id)
        plan.status = "superseded"
        order.plan_revision = 2
        await db.commit()
    inn = _inn()

    with pytest.raises(ef.EffectFenceRejected, match="plan_superseded"):
        await _commit_effect(factory, token, inn)
    assert await _effects(factory, inn) == 0


@pytest.mark.asyncio
async def test_a_request_without_a_fence_is_unchanged(factory):
    inn = _inn()
    async with factory() as db:
        ef.install_effect_fence(db)
        db.add(Party(name="Человек", inn=inn))
        await db.commit()
    assert await _effects(factory, inn) == 1


@pytest.mark.asyncio
async def test_canceling_a_parent_cancels_its_pending_child(factory):
    async with factory() as db:
        parent = await create_work_order(db, owner_key="fence-owner", objective="Родитель")
        child = await create_work_order(
            db, owner_key="fence-owner", objective="Ребёнок", parent_id=parent.id
        )
        grandchild = await create_work_order(
            db, owner_key="fence-owner", objective="Внук", parent_id=child.id
        )
        await transition_work_order(db, parent, "planning", actor="test")
        await transition_work_order(db, child, "planning", actor="test")
        await db.commit()
        parent_id, child_id, grandchild_id = parent.id, child.id, grandchild.id

    async with factory() as db:
        parent = await db.get(WorkOrder, parent_id, with_for_update=True)
        canceled = await cancel_work_order(db, parent, actor="owner")
        await db.commit()

    assert set(canceled) == {str(child_id), str(grandchild_id)}
    async with factory() as db:
        for oid in (parent_id, child_id, grandchild_id):
            assert (await db.get(WorkOrder, oid)).status == "canceled"


def _app():
    async def endpoint(_request: Request):
        bound = ef.current_request_fence()
        return JSONResponse({"fenced": bound is not None})

    app = Starlette(
        routes=[
            Route("/api/thing", endpoint, methods=["POST"]),
            Route("/api/agent/cap/thing", endpoint, methods=["POST"]),
        ]
    )
    app.add_middleware(ef.EffectFenceMiddleware)
    return app


def test_middleware_binds_a_valid_fence_and_refuses_a_forged_one(monkeypatch):
    monkeypatch.setattr(settings, "agent_service_key", "fence-service-key")
    token = _token(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    client = TestClient(_app())
    good = {ef.EFFECT_FENCE_HEADER: token, "X-API-Key": "fence-service-key"}

    assert client.post("/api/thing", headers=good).json() == {"fenced": True}
    assert client.post("/api/thing").json() == {"fenced": False}
    forged = client.post("/api/thing", headers={**good, ef.EFFECT_FENCE_HEADER: token[:-2] + "00"})
    assert forged.status_code == 409
    no_key = client.post("/api/thing", headers={ef.EFFECT_FENCE_HEADER: token})
    assert no_key.status_code == 409
    # The gateway only relays; its own request is not the effect.
    assert client.post("/api/agent/cap/thing", headers=good).json() == {"fenced": False}


def test_the_gateway_relays_the_fence_to_the_recipient():
    from app.api.capability_router import _relayed_handoff

    class _Req:
        headers = {ef.EFFECT_FENCE_HEADER: "token"}

    assert _relayed_handoff("invoices", "update", _Req()) == {ef.EFFECT_FENCE_HEADER: "token"}


def test_the_budget_context_issues_a_fence_for_its_attempt():
    from app.ai.work_budget_context import WorkBudgetContext

    ids = [uuid.uuid4() for _ in range(4)]
    context = WorkBudgetContext(
        work_order_id=ids[0],
        step_id=ids[1],
        attempt_id=ids[2],
        session_factory=None,
        fence_plan_id=ids[3],
        fence_plan_revision=3,
    )
    fence = ef.verify_effect_fence(context.effect_fence_headers("tool:k")[ef.EFFECT_FENCE_HEADER])
    assert (fence.work_order_id, fence.attempt_id, fence.plan_revision) == (ids[0], ids[2], 3)
    assert fence.operation_key == "tool:k"
    bare = WorkBudgetContext(
        work_order_id=ids[0], step_id=ids[1], attempt_id=ids[2], session_factory=None
    )
    assert bare.effect_fence_headers("tool:k") == {}


@pytest.mark.asyncio
async def test_a_second_commit_of_the_same_request_is_not_refused(factory):
    """email.send queues the SMTP task between its two commits: refusing the
    second one after a lease change would erase the record of a sent mail."""
    order_id, plan_id, step_id, attempt_id = await _claimed(factory)
    token = _token(order_id, plan_id, step_id, attempt_id)
    bound = ef._RequestFence(fence=ef.verify_effect_fence(token), method="POST", path="/api/x")
    reset = ef._request_fence.set(bound)
    first, second = _inn(), _inn()
    try:
        async with factory() as db:
            ef.install_effect_fence(db)
            db.add(Party(name="Первый", inn=first))
            await db.commit()
            # The lease is lost after the decision point.
            step = await db.get(WorkStep, step_id)
            step.lease_expires_at = utcnow() - timedelta(seconds=1)
            db.add(Party(name="Второй", inn=second))
            await db.commit()
    finally:
        ef._request_fence.reset(reset)
    assert await _effects(factory, first) == 1
    assert await _effects(factory, second) == 1
