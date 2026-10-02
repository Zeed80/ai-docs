"""E21.2b2 budget accounting for generic capability WorkSteps."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai import work_budget_context
from app.db.models import (
    Approval,
    ApprovalStatus,
    WorkOrder,
    WorkStep,
    WorkStepAttempt,
    WorkToolCall,
)
from app.db.work_budget_models import WorkBudgetReservation
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import (
    apply_approval_decision,
    claim_ready_step,
    create_single_step_plan,
    create_work_order,
    create_work_plan,
    utcnow,
)
from app.tasks import work_orders


async def _claimed_capability(
    test_engine,
    *,
    budgets=None,
    bind_ledger=True,
    dependent=False,
):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(
            db,
            owner_key="budget-work-order-owner",
            objective="Run one budgeted capability",
            budgets=budgets,
        )
        if bind_ledger:
            await initialize_budget_ledger(db, order.id)
        if dependent:
            _, steps = await create_work_plan(
                db,
                order,
                steps=[
                    {
                        "step_key": "dispatch",
                        "title": "Dispatch capability",
                        "kind": "capability",
                        "capability": "documents",
                        "action": "list",
                        "input": {},
                    },
                    {
                        "step_key": "tail",
                        "title": "Must not run",
                        "kind": "capability",
                        "capability": "documents",
                        "action": "list",
                        "input": {},
                        "depends_on": ["dispatch"],
                    },
                ],
            )
            step = steps[0]
            dependent_id = steps[1].id
        else:
            _, step = await create_single_step_plan(
                db,
                order,
                kind="capability",
                title="Dispatch capability",
                input_data={},
                capability="documents",
                action="list",
            )
            dependent_id = None
        order_id = order.id
        step_id = step.id
        await db.commit()
    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="budget-work-order", work_order_id=order_id)
        assert claimed is not None
        _, _, attempt = claimed
        attempt_id = attempt.id
        await db.commit()
    return factory, order_id, step_id, attempt_id, dependent_id


def _install_http(monkeypatch, outcomes, effects, *, exit_error=None):
    remaining = list(outcomes)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            if exit_error is not None:
                raise exit_error
            return None

        async def post(self, url, **kwargs):
            effects.append((url, kwargs))
            outcome = remaining.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            if callable(outcome):
                return await outcome()
            return outcome

    monkeypatch.setattr(work_orders.httpx, "AsyncClient", lambda *args, **kwargs: Client())
    monkeypatch.setattr("app.ai.orchestrator._agent_headers", lambda: {})


async def _tool_reservations(factory, order_ids):
    if not isinstance(order_ids, (list, tuple)):
        order_ids = [order_ids]
    async with factory() as db:
        return list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.work_order_id.in_(order_ids),
                    WorkBudgetReservation.dimension == "tool_attempts",
                )
            )
        )


async def _run(factory, step_id, attempt_id):
    return await work_orders.execute_claimed_step(
        step_id,
        attempt_id,
        schedule_verification=False,
        session_factory=factory,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budgets, bind_ledger, expected_code",
    [
        ({"max_tool_attempts": 0}, True, "tool_attempt_budget_exceeded"),
        (None, False, "legacy_budget_baseline_required"),
    ],
)
async def test_zero_and_legacy_budget_stop_before_capability_http(
    test_engine, monkeypatch, budgets, bind_ledger, expected_code
):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(
        test_engine, budgets=budgets, bind_ledger=bind_ledger
    )
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"items": []})], effects)

    assert not await _run(factory, step_id, attempt_id)
    assert effects == []
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        assert order.status == "blocked"
        assert order.blocker["code"] == expected_code


@pytest.mark.asyncio
async def test_tool_reservation_database_failure_stops_before_http(test_engine, monkeypatch):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(test_engine)
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"items": []})], effects)
    real_reserve = work_budget_context.reserve_budget_for_dispatch

    async def broken_tool_reserve(*args, **kwargs):
        if kwargs.get("dimension") == "tool_attempts":
            raise RuntimeError("budget database unavailable")
        return await real_reserve(*args, **kwargs)

    monkeypatch.setattr(work_budget_context, "reserve_budget_for_dispatch", broken_tool_reserve)
    assert not await _run(factory, step_id, attempt_id)
    assert effects == []
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        assert order.blocker["code"] == "tool_budget_reservation_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["headers", "client_enter"])
async def test_local_capability_preflight_costs_zero(test_engine, monkeypatch, mode):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(test_engine)
    effects = []
    if mode == "headers":
        monkeypatch.setattr(
            "app.ai.orchestrator._agent_headers",
            lambda: (_ for _ in ()).throw(RuntimeError("headers unavailable")),
        )
        monkeypatch.setattr(
            work_orders.httpx,
            "AsyncClient",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no client")),
        )
    else:

        class Client:
            async def __aenter__(self):
                raise RuntimeError("client unavailable")

            async def __aexit__(self, *args):
                return None

        monkeypatch.setattr("app.ai.orchestrator._agent_headers", lambda: {})
        monkeypatch.setattr(work_orders.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    assert not await _run(factory, step_id, attempt_id)
    assert effects == []
    assert await _tool_reservations(factory, order_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome, raises_crash",
    [
        (httpx.Response(404, json={"detail": "missing"}), False),
        (httpx.Response(503, json={"detail": "down"}), False),
        (httpx.ReadTimeout("recipient outcome unknown"), False),
        (KeyboardInterrupt("worker crash"), True),
    ],
)
async def test_every_dispatched_response_error_and_crash_charges_once(
    test_engine, monkeypatch, outcome, raises_crash
):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(test_engine)
    effects = []
    _install_http(monkeypatch, [outcome], effects)

    if raises_crash:
        with pytest.raises(KeyboardInterrupt):
            await _run(factory, step_id, attempt_id)
    else:
        assert not await _run(factory, step_id, attempt_id)
    assert len(effects) == 1
    reservations = await _tool_reservations(factory, order_id)
    assert len(reservations) == 1
    assert reservations[0].state == "charged"


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_known_response_survives_settlement_failure(test_engine, monkeypatch, malformed):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(test_engine)
    effects = []
    response = (
        httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"})
        if malformed
        else httpx.Response(200, json={"items": ["known"]})
    )
    _install_http(monkeypatch, [response], effects)

    async def broken_settlement(*args, **kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr(work_budget_context, "settle_budget", broken_settlement)
    assert not await _run(factory, step_id, attempt_id)
    assert len(effects) == 1
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        call = await db.scalar(select(WorkToolCall).where(WorkToolCall.attempt_id == attempt_id))
        assert order.status == "blocked"
        assert order.blocker["code"] == "tool_budget_settlement_unavailable"
        assert attempt.output["recipient_confirmed"] is True
        assert call.output == attempt.output
        if malformed:
            assert attempt.output["response_text"] == "not-json"
        else:
            assert attempt.output["result"] == {"items": ["known"]}
    reservations = await _tool_reservations(factory, order_id)
    assert reservations[0].state == "reserved"


@pytest.mark.asyncio
async def test_client_exit_cannot_mask_known_response_after_settlement_failure(
    test_engine, monkeypatch
):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(test_engine)
    effects = []
    _install_http(
        monkeypatch,
        [httpx.Response(200, json={"items": ["known"]})],
        effects,
        exit_error=RuntimeError("client exit failed"),
    )

    async def broken_settlement(*args, **kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr(work_budget_context, "settle_budget", broken_settlement)
    assert not await _run(factory, step_id, attempt_id)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        assert order.blocker["code"] == "tool_budget_settlement_unavailable"
        assert attempt.output["result"] == {"items": ["known"]}


@pytest.mark.asyncio
async def test_failed_v1_settlement_blocker_preserves_raw_http_body(test_engine, monkeypatch):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(test_engine)
    raw_body = {
        "version": 1,
        "status": "failed",
        "data": {"reason": "denied"},
        "error_code": "recipient_rejected",
    }
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json=raw_body)], effects)

    async def broken_settlement(*args, **kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr(work_budget_context, "settle_budget", broken_settlement)
    assert not await _run(factory, step_id, attempt_id)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        assert order.blocker["code"] == "tool_budget_settlement_unavailable"
        assert attempt.output["result"] == raw_body
        assert "retryable" not in attempt.output["result"]


@pytest.mark.asyncio
async def test_unconfirmed_transport_with_failed_settlement_is_not_saved_as_known(
    test_engine, monkeypatch
):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(test_engine)
    effects = []
    _install_http(monkeypatch, [httpx.ReadTimeout("unknown")], effects)

    async def broken_settlement(*args, **kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr(work_budget_context, "settle_budget", broken_settlement)
    assert not await _run(factory, step_id, attempt_id)
    async with factory() as db:
        attempt = await db.get(WorkStepAttempt, attempt_id)
        call = await db.scalar(select(WorkToolCall).where(WorkToolCall.attempt_id == attempt_id))
        assert attempt.output is None
        assert attempt.error["recipient_confirmed"] is False
        assert call.status == "outcome_unknown"
        assert call.output["recipient_confirmed"] is False
        assert "result" not in call.output


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["partial", "outcome_unknown"])
async def test_nonterminal_result_keeps_lifecycle_priority_over_settlement_blocker(
    test_engine, monkeypatch, status
):
    factory, order_id, step_id, attempt_id, dependent_id = await _claimed_capability(
        test_engine, dependent=True
    )
    body = {
        "version": 1,
        "status": status,
        "data": {"recipient": "accepted"},
        "error_code": "recipient_pending",
        "retryable": False,
        "evidence": {"receipt": "r-1"},
        "checkpoint": {"cursor": "c-1"},
    }
    effects = []
    _install_http(
        monkeypatch,
        [httpx.Response(200, json=body)],
        effects,
        exit_error=RuntimeError("client exit failed") if status == "outcome_unknown" else None,
    )

    async def broken_settlement(*args, **kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr(work_budget_context, "settle_budget", broken_settlement)
    verifier = AsyncMock()
    monkeypatch.setattr(work_orders, "verify_completed_step", verifier)
    assert not await _run(factory, step_id, attempt_id)
    verifier.assert_not_awaited()
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        step = await db.get(WorkStep, step_id)
        dependent = await db.get(WorkStep, dependent_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        assert order.status == "blocked"
        assert order.blocker["code"] == f"tool_result_{status}"
        assert order.blocker["budget_error"]["code"] == "tool_budget_settlement_unavailable"
        assert step.state == "failed"
        assert dependent.state == "pending"
        assert attempt.status == status
        assert attempt.output == body
        assert attempt.checkpoint == body["checkpoint"]


@pytest.mark.asyncio
async def test_raw_423_settlement_failure_stays_legacy_approval_observation(
    test_engine, monkeypatch
):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(test_engine)
    raw_body = {
        "detail": "approval required",
        "checkpoint": {"recipient_supplied": "must-not-be-trusted"},
    }
    effects = []
    _install_http(monkeypatch, [httpx.Response(423, json=raw_body)], effects)

    async def broken_settlement(*args, **kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr(work_budget_context, "settle_budget", broken_settlement)
    assert not await _run(factory, step_id, attempt_id)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        approval = await db.scalar(select(Approval).where(Approval.entity_id == order_id))
        assert order.status == "waiting_approval"
        assert attempt.output["result"] == raw_body
        assert attempt.checkpoint is None
        assert attempt.error["code"] == "approval_required"
        assert attempt.error["budget_error"]["code"] == "tool_budget_settlement_unavailable"
        assert "tool_result" not in approval.context
        assert approval.context["recipient_output"]["result"] == raw_body


@pytest.mark.asyncio
async def test_approval_settlement_evidence_survives_and_fresh_attempt_reserves_again(
    test_engine, monkeypatch
):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(test_engine)
    waiting = {
        "version": 1,
        "status": "waiting_approval",
        "data": {"recipient": "approval required"},
        "error_code": "approval_required",
        "retryable": False,
        "evidence": {"receipt": "approval-1"},
        "checkpoint": {"request_id": "approval-1"},
    }
    effects = []
    _install_http(
        monkeypatch,
        [
            httpx.Response(200, json=waiting),
            httpx.Response(200, json={"items": ["approved"]}),
        ],
        effects,
    )
    real_settle = work_budget_context.settle_budget
    tool_settlements = 0

    async def fail_first_tool_settlement(*args, **kwargs):
        nonlocal tool_settlements
        if str(kwargs.get("operation_key", "")).startswith("tool:"):
            tool_settlements += 1
            if tool_settlements == 1:
                raise RuntimeError("budget database unavailable")
        return await real_settle(*args, **kwargs)

    monkeypatch.setattr(work_budget_context, "settle_budget", fail_first_tool_settlement)
    assert not await _run(factory, step_id, attempt_id)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        approval = await db.scalar(select(Approval).where(Approval.entity_id == order_id))
        assert order.status == "waiting_approval"
        assert attempt.output == waiting
        assert attempt.error["budget_error"]["code"] == "tool_budget_settlement_unavailable"
        assert approval.context["tool_result"] == waiting
        assert approval.context["recipient_output"]["result"] == waiting
        approval.status = ApprovalStatus.approved
        await apply_approval_decision(
            db,
            work_order_id=order_id,
            step_id=step_id,
            approval_id=approval.id,
            approved=True,
            actor=order.owner_key,
            action_digest=approval.context["action_digest"],
        )
        # The pre-existing approval path retains the old attempt lease until
        # expiry. Simulate that ordinary expiry; do not bypass claim fencing.
        step = await db.get(WorkStep, step_id)
        step.lease_expires_at = utcnow()
        await db.commit()
    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="approved-attempt", work_order_id=order_id)
        assert claimed is not None
        _, _, approved_attempt = claimed
        approved_attempt_id = approved_attempt.id
        await db.commit()
    monkeypatch.setattr(work_orders, "verify_completed_step", AsyncMock(return_value=False))
    assert await _run(factory, step_id, approved_attempt_id)
    assert len(effects) == 2
    reservations = await _tool_reservations(factory, order_id)
    assert len(reservations) == 2
    assert {row.state for row in reservations} == {"reserved", "charged"}


@pytest.mark.asyncio
async def test_existing_work_tool_call_fence_prevents_same_attempt_replay(test_engine, monkeypatch):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(test_engine)
    effects = []

    async def delayed_response():
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"items": ["once"]})

    _install_http(monkeypatch, [delayed_response], effects)
    monkeypatch.setattr(work_orders, "verify_completed_step", AsyncMock(return_value=False))
    results = await asyncio.gather(
        _run(factory, step_id, attempt_id),
        _run(factory, step_id, attempt_id),
    )
    assert sorted(results) == [False, True]
    assert len(effects) == 1
    assert len(await _tool_reservations(factory, order_id)) == 1


@pytest.mark.asyncio
async def test_root_and_child_share_atomic_last_tool_slot(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        root = await create_work_order(
            db,
            owner_key="shared-budget-owner",
            objective="Root",
            budgets={"max_tool_attempts": 1},
        )
        child = await create_work_order(
            db,
            owner_key=root.owner_key,
            objective="Child",
            parent_id=root.id,
        )
        await initialize_budget_ledger(db, root.id)
        rows = []
        for order in (root, child):
            _, step = await create_single_step_plan(
                db,
                order,
                kind="capability",
                title="Shared slot",
                input_data={},
                capability="documents",
                action="list",
            )
            rows.append((order.id, step.id))
        await db.commit()
    attempts = []
    for index, (order_id, step_id) in enumerate(rows):
        async with factory() as db:
            claimed = await claim_ready_step(
                db, worker_id=f"shared-{index}", work_order_id=order_id
            )
            assert claimed is not None
            attempts.append((order_id, step_id, claimed[2].id))
            await db.commit()
    effects = []

    async def response():
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"items": []})

    _install_http(monkeypatch, [response], effects)
    monkeypatch.setattr(work_orders, "verify_completed_step", AsyncMock(return_value=False))
    results = await asyncio.gather(
        *(_run(factory, step_id, attempt_id) for _, step_id, attempt_id in attempts)
    )
    assert sorted(results) == [False, True]
    assert len(effects) == 1
    reservations = await _tool_reservations(factory, [item[0] for item in attempts])
    assert len(reservations) == 1
    assert reservations[0].state == "charged"


@pytest.mark.asyncio
async def test_cancellation_after_reserve_stops_before_http(test_engine, monkeypatch):
    factory, order_id, step_id, attempt_id, _ = await _claimed_capability(test_engine)
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"items": []})], effects)
    real_reserve = work_budget_context.reserve_budget_for_dispatch

    async def reserve_then_cancel(*args, **kwargs):
        reservation, created = await real_reserve(*args, **kwargs)
        if kwargs.get("dimension") == "tool_attempts":
            async with factory() as db:
                order = await db.get(WorkOrder, order_id)
                step = await db.get(WorkStep, step_id)
                order.status = "canceled"
                step.state = "canceled"
                step.lease_owner = None
                step.lease_expires_at = None
                await db.commit()
        return reservation, created

    monkeypatch.setattr(work_budget_context, "reserve_budget_for_dispatch", reserve_then_cancel)
    assert not await _run(factory, step_id, attempt_id)
    assert effects == []
    reservations = await _tool_reservations(factory, order_id)
    assert len(reservations) == 1
    assert reservations[0].state == "reserved"
