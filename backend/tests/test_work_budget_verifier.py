"""E21.2b3 shared-budget boundary for the detached semantic verifier."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai import ollama_client, work_budget_context
from app.db.models import WorkAcceptanceCriterion, WorkOrder, WorkStep, WorkStepAttempt
from app.db.work_budget_models import WorkBudgetReservation
from app.domain.work_budget_ledger import initialize_budget_ledger, reserve_budget
from app.domain.work_orders import create_single_step_plan, create_work_order, utcnow
from app.tasks import work_orders


async def _verification_case(test_engine, *, budgets=None, bind_ledger=True, parent_id=None):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(
            db,
            owner_key="verifier-owner",
            objective="Verify the completed result",
            budgets=budgets,
            parent_id=parent_id,
            acceptance_criteria=[
                {
                    "criterion_key": "semantic",
                    "description": "The result satisfies the objective",
                    "kind": "semantic",
                    "predicate": {},
                    "required": True,
                }
            ],
        )
        if bind_ledger:
            await initialize_budget_ledger(db, order.id)
        _, step = await create_single_step_plan(
            db, order, kind="agent_turn", title="Result", input_data={"prompt": "result"}
        )
        step.state = "succeeded"
        step.output = {"text": "A supported result"}
        step.finished_at = utcnow()
        order.status = "blocked"
        order.blocker = {
            "code": "independent_verification_required",
            "criteria": ["semantic"],
        }
        order_id = order.id
        criterion_id = await db.scalar(
            select(WorkAcceptanceCriterion.id).where(
                WorkAcceptanceCriterion.work_order_id == order.id
            )
        )
        assert criterion_id is not None
        await db.commit()
    return factory, order_id, criterion_id


def _install_model(monkeypatch, *, provider="ollama"):
    monkeypatch.setattr(
        "app.ai.model_resolver.get_reasoning_model",
        lambda **_kwargs: SimpleNamespace(model="verifier-model", provider=provider),
    )
    monkeypatch.setattr(ollama_client, "_ensure_gpu_free", lambda: None)


def _response(criterion_id, *, valid=True):
    content = (
        json.dumps(
            {
                "verdicts": [
                    {
                        "criterion_id": str(criterion_id),
                        "ok": True,
                        "reason": "supported",
                        "checks": ["output"],
                    }
                ]
            }
        )
        if valid
        else "not-json"
    )

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": content}}

    return Response()


def _install_http(monkeypatch, outcomes, calls, *, enter_error=None):
    remaining = list(outcomes)

    class Client:
        async def __aenter__(self):
            if enter_error is not None:
                raise enter_error
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            outcome = remaining.pop(0)
            if callable(outcome):
                return await outcome()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(ollama_client.httpx, "AsyncClient", lambda **_kwargs: Client())


async def _reservations(factory, order_id):
    async with factory() as db:
        return list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.work_order_id == order_id,
                    WorkBudgetReservation.dimension == "llm_calls",
                )
            )
        )


@pytest.mark.asyncio
async def test_verifier_charges_one_call_without_fake_attempt(test_engine, monkeypatch):
    factory, order_id, criterion_id = await _verification_case(test_engine)
    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [_response(criterion_id)], calls)

    assert await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    assert len(calls) == 1
    reservations = await _reservations(factory, order_id)
    assert sorted((row.reserved_units, row.state) for row in reservations) == [
        (0, "reserved"),
        (1, "charged"),
    ]
    async with factory() as db:
        # This order's attempts only: the test DB is shared and other tests
        # commit attempts of their own (it failed in every full run).
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkStepAttempt)
                .join(WorkStep, WorkStep.id == WorkStepAttempt.step_id)
                .where(WorkStep.work_order_id == order_id)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_parse_retry_charges_each_physical_post(test_engine, monkeypatch):
    factory, order_id, criterion_id = await _verification_case(test_engine)
    _install_model(monkeypatch)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    calls = []
    _install_http(
        monkeypatch, [_response(criterion_id, valid=False), _response(criterion_id)], calls
    )

    assert await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    assert len(calls) == 2
    physical = [row for row in await _reservations(factory, order_id) if row.reserved_units == 1]
    assert len(physical) == 2
    assert {row.state for row in physical} == {"charged"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budgets,bind_ledger,expected_code",
    [
        ({"max_llm_calls": 0}, True, "llm_call_budget_exceeded"),
        ({"token_budget": 100}, True, "token_budget_enforcement_unavailable"),
        ({"max_cost_usd": "1"}, True, "cost_budget_enforcement_unavailable"),
        (None, False, "legacy_budget_baseline_required"),
    ],
)
async def test_unprovable_budget_stops_before_gpu_and_http(
    test_engine, monkeypatch, budgets, bind_ledger, expected_code
):
    factory, order_id, criterion_id = await _verification_case(
        test_engine, budgets=budgets, bind_ledger=bind_ledger
    )
    _install_model(monkeypatch)
    gpu_checks = []
    monkeypatch.setattr(ollama_client, "_ensure_gpu_free", lambda: gpu_checks.append(True))
    calls = []
    _install_http(monkeypatch, [_response(criterion_id)], calls)

    assert not await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    assert calls == []
    assert gpu_checks == []
    async with factory() as db:
        assert (await db.get(WorkOrder, order_id)).blocker["code"] == expected_code


@pytest.mark.asyncio
async def test_client_preparation_failure_consumes_no_budget_slot(test_engine, monkeypatch):
    factory, order_id, _ = await _verification_case(test_engine)
    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [], calls, enter_error=RuntimeError("client init failed"))

    assert not await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    assert await _reservations(factory, order_id) == []
    async with factory() as db:
        assert (await db.get(WorkOrder, order_id)).blocker["code"] == "verification_provider_failed"


@pytest.mark.asyncio
async def test_unsupported_provider_stops_before_gpu_or_dispatch(test_engine, monkeypatch):
    factory, order_id, criterion_id = await _verification_case(test_engine)
    _install_model(monkeypatch, provider="openai")
    gpu_checks = []
    monkeypatch.setattr(ollama_client, "_ensure_gpu_free", lambda: gpu_checks.append(True))
    calls = []
    _install_http(monkeypatch, [_response(criterion_id)], calls)

    assert not await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    assert calls == []
    assert gpu_checks == []
    assert await _reservations(factory, order_id) == []
    async with factory() as db:
        assert (await db.get(WorkOrder, order_id)).blocker["code"] == (
            "verification_provider_unsupported"
        )


@pytest.mark.asyncio
async def test_reservation_failure_stops_before_http(test_engine, monkeypatch):
    factory, order_id, criterion_id = await _verification_case(test_engine)
    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [_response(criterion_id)], calls)

    async def broken_reserve(*_args, **_kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr(work_budget_context, "reserve_budget_for_dispatch", broken_reserve)
    assert not await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    assert calls == []
    async with factory() as db:
        assert (await db.get(WorkOrder, order_id)).blocker["code"] == "verification_fence_failed"


@pytest.mark.asyncio
async def test_settlement_failure_blocks_without_retry_or_verdict(test_engine, monkeypatch):
    factory, order_id, criterion_id = await _verification_case(test_engine)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id, with_for_update=True)
        order.status = "verifying"
        await db.commit()
    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [_response(criterion_id)], calls)

    async def broken_settle(*_args, **_kwargs):
        raise RuntimeError("settlement database unavailable")

    monkeypatch.setattr(work_budget_context, "settle_llm_call_with_usage_receipt", broken_settle)
    assert not await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    assert len(calls) == 1
    physical = [row for row in await _reservations(factory, order_id) if row.reserved_units == 1]
    assert len(physical) == 1
    assert physical[0].state == "reserved"
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        criterion = await db.get(WorkAcceptanceCriterion, criterion_id)
        assert order.status == "blocked"
        assert order.blocker["code"] == "llm_budget_settlement_unavailable"
        assert criterion.status == "pending"
    assert not await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_dispatches_once(test_engine, monkeypatch):
    factory, order_id, criterion_id = await _verification_case(test_engine)
    _install_model(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow_response():
        entered.set()
        await release.wait()
        return _response(criterion_id)

    _install_http(monkeypatch, [slow_response], calls)
    winner = asyncio.create_task(
        work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    )
    await entered.wait()
    loser = await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    release.set()

    assert loser is False
    assert await winner is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_during_request_discards_verdict_without_overwrite(test_engine, monkeypatch):
    factory, order_id, criterion_id = await _verification_case(test_engine)
    _install_model(monkeypatch)
    calls = []

    async def cancel_then_respond():
        async with factory() as db:
            order = await db.get(WorkOrder, order_id, with_for_update=True)
            order.status = "canceled"
            order.blocker = {"code": "canceled_by_owner"}
            await db.commit()
        return _response(criterion_id)

    _install_http(monkeypatch, [cancel_then_respond], calls)
    assert not await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        assert order.status == "canceled"
        assert order.blocker == {"code": "canceled_by_owner"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "output", "criterion"])
async def test_stale_authoritative_snapshot_discards_verdict(test_engine, monkeypatch, change):
    factory, order_id, criterion_id = await _verification_case(test_engine)
    _install_model(monkeypatch)
    calls = []

    async def mutate_then_respond():
        async with factory() as db:
            order = await db.get(WorkOrder, order_id, with_for_update=True)
            if change == "revision":
                order.plan_revision += 1
            elif change == "output":
                step = await db.scalar(select(WorkStep).where(WorkStep.work_order_id == order_id))
                step.output = {"text": "changed while verifier was running"}
            else:
                criterion = await db.get(WorkAcceptanceCriterion, criterion_id)
                criterion.predicate = {"changed": True}
            await db.commit()
        return _response(criterion_id)

    _install_http(monkeypatch, [mutate_then_respond], calls)
    assert not await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    async with factory() as db:
        assert (await db.get(WorkAcceptanceCriterion, criterion_id)).status == "pending"


@pytest.mark.asyncio
async def test_provider_crash_is_charged_and_same_snapshot_cannot_replay(test_engine, monkeypatch):
    factory, order_id, criterion_id = await _verification_case(test_engine)
    _install_model(monkeypatch)
    calls = []

    class ProviderCrash(BaseException):
        pass

    _install_http(monkeypatch, [ProviderCrash("worker crash")], calls)
    with pytest.raises(ProviderCrash):
        await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    assert len(calls) == 1
    physical = [row for row in await _reservations(factory, order_id) if row.reserved_units == 1]
    assert len(physical) == 1
    assert physical[0].state == "charged"

    assert not await work_orders.verify_semantic_criteria(order_id, session_factory=factory)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_child_verifier_shares_parent_last_llm_slot(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        parent = await create_work_order(
            db,
            owner_key="verifier-owner",
            objective="Parent",
            budgets={"max_llm_calls": 1},
        )
        await initialize_budget_ledger(db, parent.id)
        parent_id = parent.id
        await db.commit()
    factory, child_id, criterion_id = await _verification_case(
        test_engine,
        budgets={"max_llm_calls": 1},
        bind_ledger=True,
        parent_id=parent_id,
    )
    await reserve_budget(
        factory,
        work_order_id=parent_id,
        operation_key="parent-used-last-slot",
        dimension="llm_calls",
        units=1,
        request_digest="a" * 64,
    )
    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [_response(criterion_id)], calls)

    assert not await work_orders.verify_semantic_criteria(child_id, session_factory=factory)
    assert calls == []
    async with factory() as db:
        assert (await db.get(WorkOrder, child_id)).blocker["code"] == "llm_call_budget_exceeded"
