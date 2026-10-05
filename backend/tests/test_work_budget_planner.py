"""E21.2b4 shared-budget boundary for detached direct-Ollama planning."""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai import ollama_client, planner_budget_context
from app.db.models import WorkOrder, WorkPlan, WorkStep, WorkStepAttempt
from app.db.work_budget_models import WorkBudgetReservation
from app.domain.work_budget_ledger import initialize_budget_ledger, reserve_budget
from app.domain.work_orders import create_single_step_plan, create_work_order, utcnow
from app.domain.work_planning import plan_work_order_detached
from app.tasks import work_orders


async def _planning_case(
    test_engine, *, budgets=None, bind_ledger=True, completed_step=False, parent_id=None
):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(
            db,
            owner_key="planner-owner",
            objective="Build a grounded execution plan",
            constraints={"source": "project"},
            budgets=budgets,
            parent_id=parent_id,
        )
        if bind_ledger:
            await initialize_budget_ledger(db, order.id)
        if completed_step:
            _, step = await create_single_step_plan(
                db,
                order,
                kind="agent_turn",
                title="Prior result",
                input_data={"prompt": "prior"},
            )
            step.state = "succeeded"
            step.output = {"text": "authoritative prior result"}
            step.finished_at = utcnow()
            order.status = "replanning"
        else:
            order.status = "planning"
        order_id = order.id
        await db.commit()
    return factory, order_id


def _install_model(monkeypatch, *, provider="ollama"):
    model = f"planner-model-{uuid.uuid4()}"
    monkeypatch.setattr(
        "app.ai.model_resolver.get_reasoning_model",
        lambda **_kwargs: SimpleNamespace(model=model, provider=provider),
    )
    monkeypatch.setattr(ollama_client, "_ensure_gpu_free", lambda: None)


def _response(*, valid=True):
    content = (
        json.dumps(
            {
                "assumptions": [],
                "steps": [
                    {
                        "step_key": "execute",
                        "title": "Execute safely",
                        "kind": "agent_turn",
                        "input": {"prompt": "execute"},
                    }
                ],
                "verification_plan": {"mode": "deterministic_then_independent"},
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
async def test_planner_charges_one_call_without_fake_attempt(test_engine, monkeypatch):
    factory, order_id = await _planning_case(test_engine)
    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [_response()], calls)

    assert await plan_work_order_detached(order_id, session_factory=factory)
    assert len(calls) == 1
    reservations = await _reservations(factory, order_id)
    assert sorted((row.reserved_units, row.state) for row in reservations) == [
        (0, "reserved"),
        (1, "charged"),
    ]
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        assert order.status == "ready"
        assert order.plan_revision == 1
        assert await db.scalar(select(func.count()).select_from(WorkStepAttempt)) == 0


@pytest.mark.asyncio
async def test_parse_retry_charges_each_physical_post(test_engine, monkeypatch):
    factory, order_id = await _planning_case(test_engine)
    _install_model(monkeypatch)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    calls = []
    _install_http(monkeypatch, [_response(valid=False), _response()], calls)

    assert await plan_work_order_detached(order_id, session_factory=factory)
    physical = [row for row in await _reservations(factory, order_id) if row.reserved_units == 1]
    assert len(calls) == len(physical) == 2
    assert {row.state for row in physical} == {"charged"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budgets,bind_ledger,provider,expected_code",
    [
        ({"max_llm_calls": 0}, True, "ollama", "llm_call_budget_exceeded"),
        ({"token_budget": 100}, True, "ollama", "token_budget_enforcement_unavailable"),
        ({"max_cost_usd": "1"}, True, "ollama", "cost_budget_enforcement_unavailable"),
        (None, False, "ollama", "legacy_budget_baseline_required"),
        (None, True, "openai", "planning_provider_unsupported"),
    ],
)
async def test_unprovable_or_unsupported_stops_before_gpu_and_http(
    test_engine, monkeypatch, budgets, bind_ledger, provider, expected_code
):
    factory, order_id = await _planning_case(test_engine, budgets=budgets, bind_ledger=bind_ledger)
    _install_model(monkeypatch, provider=provider)
    gpu_checks = []
    monkeypatch.setattr(ollama_client, "_ensure_gpu_free", lambda: gpu_checks.append(True))
    calls = []
    _install_http(monkeypatch, [_response()], calls)

    assert not await plan_work_order_detached(order_id, session_factory=factory)
    assert calls == []
    assert gpu_checks == []
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        assert order.status == "blocked"
        assert order.blocker["code"] == expected_code


@pytest.mark.asyncio
async def test_reservation_failure_stops_before_http(test_engine, monkeypatch):
    factory, order_id = await _planning_case(test_engine)
    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [_response()], calls)

    async def broken_reserve(*_args, **_kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr(planner_budget_context, "reserve_budget_for_dispatch", broken_reserve)
    assert not await plan_work_order_detached(order_id, session_factory=factory)
    assert calls == []
    async with factory() as db:
        assert (await db.get(WorkOrder, order_id)).blocker["code"] == "planning_fence_failed"


@pytest.mark.asyncio
async def test_settlement_failure_blocks_without_plan_or_fallback(test_engine, monkeypatch):
    factory, order_id = await _planning_case(test_engine)
    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [_response()], calls)

    async def broken_settle(*_args, **_kwargs):
        raise RuntimeError("settlement unavailable")

    monkeypatch.setattr(planner_budget_context, "settle_llm_call_with_usage_receipt", broken_settle)
    assert not await plan_work_order_detached(order_id, session_factory=factory)
    assert len(calls) == 1
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        assert order.status == "blocked"
        assert order.plan_revision == 0
        assert order.blocker["code"] == "llm_budget_settlement_unavailable"
        assert (
            await db.scalar(
                select(func.count()).select_from(WorkPlan).where(WorkPlan.work_order_id == order_id)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_ordinary_provider_failure_keeps_existing_fallback_policy(test_engine, monkeypatch):
    factory, order_id = await _planning_case(test_engine)
    _install_model(monkeypatch)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    calls = []
    _install_http(monkeypatch, [RuntimeError("down")] * 3, calls)

    assert await plan_work_order_detached(order_id, session_factory=factory)
    assert len(calls) == 1
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        assert order.status == "ready"
        assert order.metadata_["planner_fallback_streak"] == 1
        assert "down" in order.metadata_["last_planner_error"]


@pytest.mark.asyncio
async def test_concurrent_duplicate_dispatches_once(test_engine, monkeypatch):
    from dataclasses import replace

    from app.domain import work_planning as planning_module

    factory, order_id = await _planning_case(test_engine)
    _install_model(monkeypatch)
    real_freeze = planning_module._freeze_planner_request
    freeze_no = 0

    async def freeze_with_dynamic_enrichment(snapshot):
        nonlocal freeze_no
        frozen = await real_freeze(snapshot)
        freeze_no += 1
        return replace(frozen, digest=("a" if freeze_no == 1 else "b") * 64)

    monkeypatch.setattr(planning_module, "_freeze_planner_request", freeze_with_dynamic_enrichment)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow_response():
        entered.set()
        await release.wait()
        return _response()

    _install_http(monkeypatch, [slow_response], calls)
    winner = asyncio.create_task(plan_work_order_detached(order_id, session_factory=factory))
    await entered.wait()
    loser = await plan_work_order_detached(order_id, session_factory=factory)
    release.set()

    assert loser is False
    assert await winner is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_crash_evidence_forbids_replay(test_engine, monkeypatch):
    factory, order_id = await _planning_case(test_engine)
    _install_model(monkeypatch)
    calls = []

    class ProviderCrash(BaseException):
        pass

    _install_http(monkeypatch, [ProviderCrash("worker crash")], calls)
    with pytest.raises(ProviderCrash):
        await plan_work_order_detached(order_id, session_factory=factory)
    assert not await plan_work_order_detached(order_id, session_factory=factory)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["status", "revision", "output", "constraints", "blocker"])
async def test_stale_authoritative_snapshot_discards_plan(test_engine, monkeypatch, change):
    factory, order_id = await _planning_case(test_engine, completed_step=change == "output")
    _install_model(monkeypatch)
    calls = []

    async def mutate_then_respond():
        async with factory() as db:
            order = await db.get(WorkOrder, order_id, with_for_update=True)
            if change == "status":
                order.status = "canceled"
            elif change == "revision":
                order.plan_revision += 1
            elif change == "output":
                step = await db.scalar(
                    select(WorkStep).where(
                        WorkStep.work_order_id == order_id, WorkStep.state == "succeeded"
                    )
                )
                step.output = {"text": "changed during planning"}
            elif change == "constraints":
                order.constraints = {"source": "changed"}
            else:
                order.blocker = {"code": "changed"}
            await db.commit()
        return _response()

    _install_http(monkeypatch, [mutate_then_respond], calls)
    assert not await plan_work_order_detached(order_id, session_factory=factory)
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        if change != "output":
            assert order.plan_revision == (1 if change == "revision" else 0)
        else:
            assert order.plan_revision == 1
        plan_count = await db.scalar(
            select(func.count()).select_from(WorkPlan).where(WorkPlan.work_order_id == order_id)
        )
        assert plan_count == (1 if change == "output" else 0)


@pytest.mark.asyncio
async def test_worker_entrypoint_uses_injected_detached_factory(test_engine, monkeypatch):
    factory, order_id = await _planning_case(test_engine)
    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [_response()], calls)

    assert await work_orders._plan_order(order_id, session_factory=factory)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_child_planner_shares_parent_last_llm_slot(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        parent = await create_work_order(
            db,
            owner_key="planner-owner",
            objective="Parent",
            budgets={"max_llm_calls": 1},
        )
        await initialize_budget_ledger(db, parent.id)
        parent_id = parent.id
        await db.commit()
    factory, child_id = await _planning_case(
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
    _install_http(monkeypatch, [_response()], calls)

    assert not await plan_work_order_detached(child_id, session_factory=factory)
    assert calls == []


@pytest.mark.asyncio
async def test_api_fresh_manual_plan_initializes_ledger(client, db_session):
    response = await client.post(
        "/api/work-orders",
        json={
            "objective": "Manual plan",
            "steps": [{"step_key": "one", "title": "One", "kind": "agent_turn"}],
        },
    )
    assert response.status_code == 201, response.text
    order = await db_session.get(WorkOrder, uuid.UUID(response.json()["id"]))
    assert order.budget_ledger_id is not None


@pytest.mark.asyncio
async def test_api_rejects_historic_unbound_parent_without_zero_baseline(client, db_session):
    parent = await create_work_order(
        db_session,
        owner_key="dev-user",
        objective="Historic parent",
    )
    await create_single_step_plan(
        db_session,
        parent,
        kind="agent_turn",
        title="Historic",
        input_data={"prompt": "historic"},
    )
    await db_session.commit()

    response = await client.post(
        "/api/work-orders",
        json={"objective": "Child", "parent_id": str(parent.id)},
    )
    assert response.status_code == 409
    assert "baseline" in response.json()["detail"].lower()
    assert (
        await db_session.scalar(
            select(func.count()).select_from(WorkOrder).where(WorkOrder.objective == "Child")
        )
        == 0
    )


@pytest.mark.asyncio
async def test_api_hides_foreign_parent_before_creating_child(client, db_session):
    from app.auth.jwt import get_current_user
    from app.auth.models import UserInfo, UserRole
    from app.main import app

    parent = await create_work_order(
        db_session,
        owner_key="other-owner",
        objective="Foreign parent",
    )
    await db_session.commit()
    requester = UserInfo(
        sub="limited-user",
        email="limited@example.test",
        name="Limited",
        preferred_username="limited",
        roles=[UserRole.viewer],
    )
    app.dependency_overrides[get_current_user] = lambda: requester

    response = await client.post(
        "/api/work-orders",
        json={"objective": "Forbidden child", "parent_id": str(parent.id)},
    )
    assert response.status_code == 404
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(WorkOrder)
            .where(WorkOrder.objective == "Forbidden child")
        )
        == 0
    )


@pytest.mark.asyncio
async def test_api_run_now_budget_stop_returns_persisted_blocked_201(
    client, db_session, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "app_env", "production")
    _install_model(monkeypatch)
    calls = []
    _install_http(monkeypatch, [_response()], calls)

    response = await client.post(
        "/api/work-orders",
        json={
            "objective": "No budget",
            "budgets": {"max_llm_calls": 0},
            "run_now": True,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "blocked"
    assert response.json()["blocker"]["code"] == "llm_call_budget_exceeded"
    assert calls == []
