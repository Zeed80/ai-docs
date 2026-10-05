"""E21.2b6 durable AIRouter physical attempts use the shared ledger."""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.model_registry import ModelRegistry
from app.ai.providers import ollama as ollama_provider
from app.ai.providers.base import AIProvider
from app.ai.providers.ollama import OllamaProvider
from app.ai.router import AIRouter
from app.ai.schemas import (
    AIRequest,
    AIResponse,
    AITask,
    ChatMessage,
    Modality,
    ModelCapability,
    ProviderConfig,
    ProviderKind,
    TaskRoute,
)
from app.ai.work_budget_context import (
    BudgetExecutionStopped,
    WorkBudgetContext,
    bind_airouter_budget_context,
    current_airouter_budget_context,
)
from app.api.chat_runs import ChatRunCreate, submit_chat_run
from app.auth.jwt import _DEV_USER
from app.db.models import WorkOrder, WorkPlan, WorkStep
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.work_orders import claim_ready_step
from app.tasks.durable_chat import run_durable_chat


class Decision(BaseModel):
    ok: bool


class SequencedProvider(AIProvider):
    kind = ProviderKind.OLLAMA

    def __init__(self, outcomes, *, kind=ProviderKind.OLLAMA):
        self.kind = kind
        super().__init__(ProviderConfig(kind=kind, base_url="http://provider.test"))
        self.outcomes = list(outcomes)
        self.calls = 0

    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            outcome = await outcome()
        return AIResponse(task=request.task, provider=self.kind, model=model, text=outcome)

    async def vision(self, request: AIRequest, model: str) -> AIResponse:
        return await self.chat(request, model)

    async def embedding(self, request: AIRequest, model: str) -> AIResponse:
        return await self.chat(request, model)

    async def rerank(self, request: AIRequest, model: str) -> AIResponse:
        return await self.chat(request, model)


def _request() -> ChatRunCreate:
    return ChatRunCreate(request_id=uuid.uuid4(), content="AIRouter budget test")


async def _async_none():
    return None


async def _claimed_context(test_engine, *, budgets=None):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    body = _request()
    async with factory() as db:
        run = await submit_chat_run(body, db, _DEV_USER)
    async with factory() as db:
        order, step, attempt = await claim_ready_step(
            db, worker_id="airouter-budget-test", work_order_id=run["work_order_id"]
        )
        plan = await db.get(WorkPlan, step.plan_id)
        ledger_id = order.budget_ledger_id
        if budgets:
            ledger = await db.get(WorkBudgetLedger, ledger_id)
            for key, value in budgets.items():
                setattr(ledger, key, value)
        await db.commit()
    return (
        factory,
        run,
        WorkBudgetContext(
            work_order_id=order.id,
            step_id=step.id,
            attempt_id=attempt.id,
            session_factory=factory,
            expected_owner_key=order.owner_key,
            expected_plan_id=plan.id,
            expected_plan_revision=plan.revision,
            expected_ledger_id=ledger_id,
        ),
    )


def _router(monkeypatch, provider: AIProvider) -> AIRouter:
    model_key = "airouter_budget_ollama"
    provider_kind = provider.kind
    registry = ModelRegistry(
        providers={
            provider_kind: ProviderConfig(
                kind=provider_kind,
                base_url="http://provider.test",
                is_local=True,
            )
        },
        models={
            model_key: ModelCapability(
                name=model_key,
                provider=provider_kind,
                provider_model="budget-model",
                modalities={Modality.TEXT},
                supports_structured_output=False,
                local_only=True,
            )
        },
        routes={
            AITask.ORCHESTRATOR_PLANNING: TaskRoute(
                task=AITask.ORCHESTRATOR_PLANNING,
                fallback_chain=[model_key],
            )
        },
    )
    routing = SimpleNamespace(
        models=[model_key],
        cloud_override=False,
        local_only=True,
        allow_cloud=False,
        thinking=None,
        thinking_level=None,
    )
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda _task: routing)
    return AIRouter(registry, providers={provider_kind: provider})


def _decision_request(**updates) -> AIRequest:
    request = AIRequest(
        task=AITask.ORCHESTRATOR_PLANNING,
        messages=[ChatMessage(role="user", content="route this")],
        response_schema=Decision,
        preferred_model="airouter_budget_ollama",
        metadata={"format_max_reasks": 1},
    )
    return request.model_copy(update=updates)


async def _physical(factory, order_id):
    async with factory() as db:
        return list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.work_order_id == order_id,
                    WorkBudgetReservation.operation_key.like("llm:%"),
                )
            )
        )


@pytest.mark.asyncio
async def test_real_ollama_posts_match_reservations_on_format_retry(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    provider = OllamaProvider(
        ProviderConfig(kind=ProviderKind.OLLAMA, base_url="http://provider.test")
    )
    router = _router(monkeypatch, provider)
    bodies = [
        {"message": {"content": "not-json"}},
        {"message": {"content": '{"ok": true}'}},
    ]
    posts = []

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            posts.append((url, kwargs))
            return Response(bodies.pop(0))

    monkeypatch.setattr(ollama_provider.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr("app.ai.server_lifecycle.ensure_running", lambda _kind: _async_none())

    with bind_airouter_budget_context(context):
        response = await router.run(_decision_request())

    assert response.data == Decision(ok=True)
    reservations = await _physical(factory, run["work_order_id"])
    assert len(posts) == len(reservations) == 2
    assert {url for url, _ in posts} == {"http://provider.test/api/chat"}
    assert {row.state for row in reservations} == {"charged"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budgets", "updates", "expected_code"),
    [
        ({"max_llm_calls": Decimal(0)}, {}, "llm_call_budget_exceeded"),
        ({"max_tokens": Decimal(100)}, {}, "token_budget_enforcement_unavailable"),
        ({"max_cost_usd": Decimal("1.00")}, {}, "cost_budget_enforcement_unavailable"),
        ({}, {"images": ["data:image/png;base64,ZmFrZQ=="]}, "airouter_provider_path_unsupported"),
        (
            {},
            {"task": AITask.LONG_CONTEXT_SUMMARIZATION},
            "airouter_provider_path_unsupported",
        ),
    ],
)
async def test_unproven_paths_and_bounds_stop_before_server_or_provider(
    test_engine, monkeypatch, budgets, updates, expected_code
):
    _, _, context = await _claimed_context(test_engine, budgets=budgets)
    provider = SequencedProvider(['{"ok": true}'])
    router = _router(monkeypatch, provider)
    server_starts = []

    async def ensure_running(kind):
        server_starts.append(kind)

    monkeypatch.setattr("app.ai.server_lifecycle.ensure_running", ensure_running)
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await router.run(_decision_request(**updates))

    assert stopped.value.code == expected_code
    assert server_starts == []
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("task", [AITask.EMBEDDING, AITask.RERANKING])
async def test_retrieval_tasks_are_explicitly_unbudgeted_not_stopped(
    test_engine, monkeypatch, task
):
    """A memory/recipe lookup must not stop the durable turn (live 2026-10-05)."""
    factory, run, context = await _claimed_context(test_engine)
    provider = SequencedProvider(['{"ok": true}'])
    router = _router(monkeypatch, provider)
    monkeypatch.setattr("app.ai.server_lifecycle.ensure_running", lambda _kind: _async_none())

    with bind_airouter_budget_context(context):
        await router.run(_decision_request(task=task, response_schema=None))
        context.raise_if_stopped()

    assert provider.calls == 1
    assert await _physical(factory, run["work_order_id"]) == []


@pytest.mark.asyncio
async def test_postflight_cancel_blocks_response_and_format_recovery(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)

    async def cancel_then_fail():
        async with factory() as db:
            order = await db.get(WorkOrder, run["work_order_id"])
            order.status = "canceled"
            await db.commit()
        raise RuntimeError("provider failed after cancellation")

    provider = SequencedProvider([cancel_then_fail, '{"ok": true}'])
    router = _router(monkeypatch, provider)
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await router.run(_decision_request())

    assert stopped.value.code == "budget_execution_inactive"
    assert provider.calls == 1
    reservations = await _physical(factory, run["work_order_id"])
    assert len(reservations) == 1
    assert reservations[0].state == "charged"


@pytest.mark.asyncio
async def test_success_response_is_not_applied_after_plan_becomes_stale(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)

    async def stale_then_succeed():
        async with factory() as db:
            order = await db.get(WorkOrder, run["work_order_id"])
            plan = await db.get(WorkPlan, context.expected_plan_id)
            plan.status = "superseded"
            order.plan_revision += 1
            await db.commit()
        return '{"ok": true}'

    provider = SequencedProvider([stale_then_succeed])
    router = _router(monkeypatch, provider)
    monkeypatch.setattr("app.ai.server_lifecycle.ensure_running", lambda _kind: _async_none())
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await router.run(_decision_request())

    assert stopped.value.code == "budget_execution_inactive"
    assert provider.calls == 1
    reservations = await _physical(factory, run["work_order_id"])
    assert len(reservations) == 1
    assert reservations[0].state == "charged"


@pytest.mark.asyncio
async def test_agent_session_consumption_exhausts_same_last_slot_before_server(
    test_engine, monkeypatch
):
    _, _, context = await _claimed_context(test_engine, budgets={"max_llm_calls": Decimal(1)})
    logical_call = await context.begin_logical_call()
    operation_key = await context.prepare_provider_call(
        logical_call_no=logical_call,
        provider="ollama",
        provider_attempt=1,
        request={"source": "agent_session"},
    )
    await context.charge_provider_call(operation_key)
    provider = SequencedProvider(['{"ok": true}'])
    router = _router(monkeypatch, provider)
    server_starts = []

    async def ensure_running(kind):
        server_starts.append(kind)

    monkeypatch.setattr("app.ai.server_lifecycle.ensure_running", ensure_running)
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await router.run(_decision_request())
    assert stopped.value.code == "llm_call_budget_exceeded"
    assert server_starts == []
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_target", "expected_calls", "expected_code"),
    [
        ("reserve", 0, "llm_budget_reservation_unavailable"),
        ("settle", 1, "llm_budget_settlement_unavailable"),
    ],
)
async def test_budget_storage_failure_never_recovers_or_retries(
    test_engine, monkeypatch, failure_target, expected_calls, expected_code
):
    _, _, context = await _claimed_context(test_engine)
    provider = SequencedProvider(["not-json", '{"ok": true}'])
    router = _router(monkeypatch, provider)
    monkeypatch.setattr("app.ai.server_lifecycle.ensure_running", lambda _kind: _async_none())
    # Keep the zero-unit replay fence outside the injected physical-reservation
    # failure so the assertion targets the provider boundary itself.
    await context.assert_ready()

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("budget storage unavailable")

    monkeypatch.setattr(
        f"app.ai.work_budget_context.{failure_target}_budget_for_dispatch"
        if failure_target == "reserve"
        else "app.ai.work_budget_context.settle_llm_call_with_usage_receipt",
        unavailable,
    )
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await router.run(_decision_request())
    assert stopped.value.code == expected_code
    assert provider.calls == expected_calls


@pytest.mark.asyncio
async def test_concurrent_airouter_calls_share_atomic_last_slot(test_engine, monkeypatch):
    import asyncio

    _, _, context = await _claimed_context(test_engine, budgets={"max_llm_calls": Decimal(1)})

    async def delayed_success():
        await asyncio.sleep(0.05)
        return '{"ok": true}'

    provider = SequencedProvider([delayed_success])
    router = _router(monkeypatch, provider)
    monkeypatch.setattr("app.ai.server_lifecycle.ensure_running", lambda _kind: _async_none())
    with bind_airouter_budget_context(context):
        results = await asyncio.gather(
            router.run(_decision_request()),
            router.run(_decision_request()),
            return_exceptions=True,
        )
    assert sum(isinstance(result, AIResponse) for result in results) == 1
    failures = [result for result in results if isinstance(result, BudgetExecutionStopped)]
    assert len(failures) == 1
    assert failures[0].code == "llm_call_budget_exceeded"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_non_ollama_route_is_blocked_before_server(test_engine, monkeypatch):
    _, _, context = await _claimed_context(test_engine)
    provider = SequencedProvider(['{"ok": true}'], kind=ProviderKind.OPENAI)
    router = _router(monkeypatch, provider)
    server_starts = []

    async def ensure_running(kind):
        server_starts.append(kind)

    monkeypatch.setattr("app.ai.server_lifecycle.ensure_running", ensure_running)
    request = _decision_request(allow_cloud=True, confidential=False)
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await router.run(request)
    assert stopped.value.code == "airouter_provider_path_unsupported"
    assert server_starts == []
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_frozen_owner_plan_and_ledger_binding_rejects_coordinated_changes(
    test_engine, monkeypatch
):
    factory, run, context = await _claimed_context(test_engine)
    provider = SequencedProvider(['{"ok": true}'])
    router = _router(monkeypatch, provider)
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id)
        order.owner_key = "coordinated-attacker"
        ledger.owner_key = "coordinated-attacker"
        await db.commit()

    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await router.run(_decision_request())
    assert stopped.value.code == "budget_execution_inactive"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_same_owner_ledger_replacement_is_rejected(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    async with factory() as db:
        replacement_run = await submit_chat_run(_request(), db, _DEV_USER)
    async with factory() as db:
        replacement = await db.get(WorkOrder, replacement_run["work_order_id"])
        order = await db.get(WorkOrder, run["work_order_id"])
        assert replacement.owner_key == order.owner_key
        order.budget_ledger_id = replacement.budget_ledger_id
        await db.commit()

    provider = SequencedProvider(['{"ok": true}'])
    router = _router(monkeypatch, provider)
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await router.run(_decision_request())
    assert stopped.value.code == "budget_execution_inactive"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_durable_chat_binds_authoritative_context_for_whole_agent_turn(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        run = await submit_chat_run(_request(), db, _DEV_USER)
    async with factory() as db:
        order, step, attempt = await claim_ready_step(
            db, worker_id="airouter-bind-test", work_order_id=run["work_order_id"]
        )
        await db.commit()
    captured = []

    class Agent:
        def __init__(self, send):
            self.send = send
            self._executor = SimpleNamespace(total_tokens=0)

        def hydrate_history(self, _history):
            return None

        async def on_user_message(self, _prompt, **_kwargs):
            context = current_airouter_budget_context()
            captured.append(context)
            await self.send({"type": "text", "content": "budget-bound answer"})
            await self.send({"type": "done"})

    result = await run_durable_chat(
        order.id,
        step.id,
        attempt.id,
        session_factory=factory,
        agent_factory=Agent,
    )
    assert result["text"] == "budget-bound answer"
    assert len(captured) == 1
    assert captured[0].expected_owner_key == order.owner_key
    assert captured[0].expected_plan_id == step.plan_id
    assert captured[0].expected_plan_revision == order.plan_revision
    assert captured[0].expected_ledger_id == order.budget_ledger_id
    assert current_airouter_budget_context() is None


@pytest.mark.asyncio
async def test_task_local_binding_resets_and_missing_frozen_context_fails_closed(test_engine):
    factory, run, _ = await _claimed_context(test_engine)
    legacy_context = WorkBudgetContext(
        work_order_id=run["work_order_id"],
        step_id=(await _load_step_id(factory, run["work_order_id"])),
        attempt_id=(await _load_attempt_id(factory, run["work_order_id"])),
        session_factory=factory,
    )
    assert current_airouter_budget_context() is None
    with bind_airouter_budget_context(legacy_context):
        assert current_airouter_budget_context() is legacy_context
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await legacy_context.begin_airouter_call(
                provider="ollama", task=AITask.ORCHESTRATOR_PLANNING.value
            )
    assert stopped.value.code == "airouter_budget_context_invalid"
    assert current_airouter_budget_context() is None


@pytest.mark.asyncio
async def test_binding_resets_on_exception_and_does_not_leak_to_sibling_task(test_engine):
    _, _, context = await _claimed_context(test_engine)
    ready = __import__("asyncio").Event()
    observed = []

    async def bound_task():
        with bind_airouter_budget_context(context):
            ready.set()
            await _async_none()
            assert current_airouter_budget_context() is context
            raise RuntimeError("turn failed")

    async def sibling_task():
        await ready.wait()
        observed.append(current_airouter_budget_context())

    import asyncio

    results = await asyncio.gather(bound_task(), sibling_task(), return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    assert observed == [None]
    assert current_airouter_budget_context() is None


async def _load_step_id(factory, order_id):
    async with factory() as db:
        return await db.scalar(select(WorkStep.id).where(WorkStep.work_order_id == order_id))


async def _load_attempt_id(factory, order_id):
    from app.db.models import WorkStepAttempt

    async with factory() as db:
        return await db.scalar(
            select(WorkStepAttempt.id)
            .join(WorkStep, WorkStepAttempt.step_id == WorkStep.id)
            .where(WorkStep.work_order_id == order_id)
        )
