"""E21.2b7 direct Ollama text helpers share the durable ledger."""

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai import ollama_client, table_sql_pipeline, work_budget_context
from app.ai.work_budget_context import (
    BudgetExecutionStopped,
    DetachedVerifierBudgetContext,
    WorkBudgetContext,
    bind_airouter_budget_context,
)
from app.api.chat_runs import ChatRunCreate, submit_chat_run
from app.auth.jwt import _DEV_USER
from app.db.models import WorkOrder, WorkPlan
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.work_orders import claim_ready_step


def _request() -> ChatRunCreate:
    return ChatRunCreate(request_id=uuid.uuid4(), content="Direct text budget test")


async def _claimed_context(test_engine, *, budgets=None):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        run = await submit_chat_run(_request(), db, _DEV_USER)
    async with factory() as db:
        order, step, attempt = await claim_ready_step(
            db, worker_id="direct-text-budget-test", work_order_id=run["work_order_id"]
        )
        plan = await db.get(WorkPlan, step.plan_id)
        ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id)
        for key, value in (budgets or {}).items():
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
            expected_ledger_id=ledger.id,
        ),
    )


class _Response:
    def __init__(self, content: str):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": self.content}}


def _install_http(monkeypatch, outcomes, *, posts=None, clients=None, exit_callbacks=None):
    remaining = list(outcomes)
    remaining_exits = list(exit_callbacks or [])
    posts = posts if posts is not None else []
    clients = clients if clients is not None else []

    class Client:
        def __init__(self):
            clients.append("created")
            self.exit_callback = remaining_exits.pop(0) if remaining_exits else None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            if self.exit_callback is not None:
                await self.exit_callback()
            return None

        async def post(self, url, **kwargs):
            posts.append((url, kwargs))
            outcome = remaining.pop(0)
            if callable(outcome):
                outcome = await outcome()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(ollama_client.httpx, "AsyncClient", lambda **_kwargs: Client())
    return posts, clients


def _install_local_runtime(monkeypatch, *, provider="ollama", gpu_checks=None):
    monkeypatch.setattr(
        "app.ai.model_resolver.get_reasoning_model",
        lambda **_kwargs: SimpleNamespace(model="direct-text-model", provider=provider),
    )
    gpu_checks = gpu_checks if gpu_checks is not None else []
    monkeypatch.setattr(ollama_client, "_ensure_gpu_free", lambda: gpu_checks.append(True))
    monkeypatch.setattr(
        ollama_client,
        "_get_breaker",
        lambda model: ollama_client.CircuitBreaker(failure_threshold=99, model_name=model),
    )
    return gpu_checks


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


async def _call_leaf(helper):
    if helper == "generate":
        return await ollama_client.generate("leaf", max_retries=1)
    if helper == "json":
        return await ollama_client.generate_json(
            "leaf", model="direct-text-model", provider="ollama"
        )
    return await ollama_client.chat([{"role": "user", "content": "leaf"}])


@pytest.mark.asyncio
async def test_generate_retry_charges_each_physical_post(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(
        monkeypatch,
        [
            httpx.ConnectError("offline", request=httpx.Request("POST", "http://ollama.test")),
            _Response("ready"),
        ],
    )

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(ollama_client, "_async_sleep", no_sleep)
    with bind_airouter_budget_context(context):
        result = await ollama_client.generate("hello", max_retries=1)

    assert result.text == "ready"
    physical = await _physical(factory, run["work_order_id"])
    assert len(posts) == len(physical) == 2
    assert {row.state for row in physical} == {"charged"}


@pytest.mark.asyncio
async def test_reasoning_leaf_starts_one_logical_call_and_one_charge(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(monkeypatch, [_Response("reasoned")])
    claude_calls = []

    async def claude(*_args, **_kwargs):
        claude_calls.append("called")
        return "unsafe"

    monkeypatch.setattr(ollama_client, "_claude_generate", claude)
    monkeypatch.setattr(ollama_client.settings, "ai_reasoning_backend", "claude")
    monkeypatch.setattr(ollama_client.settings, "anthropic_api_key", "configured")

    with bind_airouter_budget_context(context):
        result = await ollama_client.reasoning_generate("reason")

    assert result == "reasoned"
    physical = await _physical(factory, run["work_order_id"])
    assert len(posts) == len(physical) == 1
    assert ":l1:" in physical[0].operation_key
    assert claude_calls == []


@pytest.mark.asyncio
async def test_chat_success_charges_one_physical_post(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(monkeypatch, [_Response("chat result")])

    with bind_airouter_budget_context(context):
        result = await ollama_client.chat([{"role": "user", "content": "chat"}])

    assert result.text == "chat result"
    physical = await _physical(factory, run["work_order_id"])
    assert len(posts) == len(physical) == 1
    assert physical[0].state == "charged"


@pytest.mark.asyncio
async def test_generate_json_parse_retry_charges_each_post(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(monkeypatch, [_Response("not-json"), _Response('{"ok": true}')])

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with bind_airouter_budget_context(context):
        result = await ollama_client.generate_json(
            "json", model="direct-text-model", provider="ollama"
        )

    assert result == {"ok": True}
    physical = await _physical(factory, run["work_order_id"])
    assert len(posts) == len(physical) == 2
    assert {row.state for row in physical} == {"charged"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budgets", "helper", "expected_code"),
    [
        ({"max_llm_calls": Decimal(0)}, "generate", "llm_call_budget_exceeded"),
        ({"max_tokens": Decimal(100)}, "json", "token_budget_enforcement_unavailable"),
        ({"max_cost_usd": Decimal("1")}, "chat", "cost_budget_enforcement_unavailable"),
    ],
)
async def test_zero_and_unproven_caps_stop_before_gpu_client_or_http(
    test_engine, monkeypatch, budgets, helper, expected_code
):
    _, _, context = await _claimed_context(test_engine, budgets=budgets)
    gpu_checks = _install_local_runtime(monkeypatch)
    posts, clients = _install_http(monkeypatch, [_Response("unused")])

    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            if helper == "generate":
                await ollama_client.generate("blocked")
            elif helper == "json":
                await ollama_client.generate_json(
                    "blocked", model="direct-text-model", provider="ollama"
                )
            else:
                await ollama_client.chat([{"role": "user", "content": "blocked"}])

    assert stopped.value.code == expected_code
    assert gpu_checks == []
    assert clients == []
    assert posts == []


@pytest.mark.asyncio
async def test_reasoning_unsupported_provider_never_uses_legacy_fallback(test_engine, monkeypatch):
    _, _, context = await _claimed_context(test_engine)
    gpu_checks = _install_local_runtime(monkeypatch, provider="openai")
    posts, clients = _install_http(monkeypatch, [_Response("unused")])
    claude_calls = []

    async def claude(*_args, **_kwargs):
        claude_calls.append("called")
        return "unsafe"

    monkeypatch.setattr(ollama_client, "_claude_generate", claude)
    monkeypatch.setattr(ollama_client.settings, "ai_reasoning_backend", "claude")
    monkeypatch.setattr(ollama_client.settings, "anthropic_api_key", "configured")

    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await ollama_client.reasoning_generate("blocked")

    assert stopped.value.code == "direct_text_provider_unsupported"
    assert claude_calls == []
    assert gpu_checks == []
    assert clients == []
    assert posts == []


@pytest.mark.asyncio
async def test_reservation_failure_is_sticky_before_client_and_never_retries(
    test_engine, monkeypatch
):
    _, _, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    await context.assert_ready()
    posts, clients = _install_http(monkeypatch, [_Response("unused")])

    async def broken_reserve(*_args, **_kwargs):
        raise RuntimeError("reservation database unavailable")

    monkeypatch.setattr(work_budget_context, "reserve_budget_for_dispatch", broken_reserve)
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as first:
            await ollama_client.generate("blocked", max_retries=2)
        with pytest.raises(BudgetExecutionStopped) as repeated:
            await ollama_client.generate("still blocked", max_retries=2)

    assert first.value.code == "llm_budget_reservation_unavailable"
    assert repeated.value is first.value
    assert clients == []
    assert posts == []


@pytest.mark.asyncio
async def test_settlement_failure_stops_after_one_post_without_retry(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(monkeypatch, [_Response("dispatched"), _Response("unsafe retry")])

    async def broken_settle(*_args, **_kwargs):
        raise RuntimeError("settlement database unavailable")

    monkeypatch.setattr(work_budget_context, "settle_llm_call_with_usage_receipt", broken_settle)
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await ollama_client.generate("one post", max_retries=1)

    assert stopped.value.code == "llm_budget_settlement_unavailable"
    assert len(posts) == 1
    physical = await _physical(factory, run["work_order_id"])
    assert len(physical) == 1
    assert physical[0].state == "reserved"


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["generate", "json", "chat"])
async def test_postflight_overrides_provider_error_after_cancel(test_engine, monkeypatch, helper):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)

    async def cancel_then_raise():
        async with factory() as db:
            order = await db.get(WorkOrder, run["work_order_id"])
            order.status = "canceled"
            await db.commit()
        raise RuntimeError("provider failed after cancellation")

    posts, _ = _install_http(monkeypatch, [cancel_then_raise, _Response("unsafe retry")])
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await _call_leaf(helper)

    assert stopped.value.code == "budget_execution_inactive"
    assert len(posts) == 1
    physical = await _physical(factory, run["work_order_id"])
    assert len(physical) == 1
    assert physical[0].state == "charged"


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["generate", "json", "chat"])
async def test_postflight_rechecks_after_client_close(test_engine, monkeypatch, helper):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)

    async def stale_on_close():
        async with factory() as db:
            order = await db.get(WorkOrder, run["work_order_id"])
            plan = await db.get(WorkPlan, context.expected_plan_id)
            plan.status = "superseded"
            order.plan_revision += 1
            await db.commit()

    content = '{"ok": true}' if helper == "json" else "success"
    posts, _ = _install_http(monkeypatch, [_Response(content)], exit_callbacks=[stale_on_close])
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await _call_leaf(helper)

    assert stopped.value.code == "budget_execution_inactive"
    assert len(posts) == 1
    physical = await _physical(factory, run["work_order_id"])
    assert len(physical) == 1
    assert physical[0].state == "charged"


@pytest.mark.asyncio
async def test_shared_last_slot_reaches_optional_title_handler_as_typed_stop(
    test_engine, monkeypatch
):
    factory, run, context = await _claimed_context(
        test_engine, budgets={"max_llm_calls": Decimal(1)}
    )
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(monkeypatch, [_Response("SELECT id FROM invoices")])
    monkeypatch.setattr(table_sql_pipeline, "_get_schema_context", lambda: "schema")

    async def rows(_sql, *, max_rows):
        return [{"value": max_rows}]

    monkeypatch.setattr(table_sql_pipeline, "execute_sql", rows)
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await table_sql_pipeline.build_table_from_task("show one", limit=1)

    assert stopped.value.code == "llm_call_budget_exceeded"
    assert len(posts) == 1
    assert len(await _physical(factory, run["work_order_id"])) == 1


@pytest.mark.asyncio
async def test_stale_title_response_is_not_swallowed_by_optional_handler(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    monkeypatch.setattr(table_sql_pipeline, "_get_schema_context", lambda: "schema")

    async def rows(_sql, *, max_rows):
        return [{"value": max_rows}]

    async def stale_title_on_close():
        async with factory() as db:
            order = await db.get(WorkOrder, run["work_order_id"])
            order.status = "canceled"
            await db.commit()

    monkeypatch.setattr(table_sql_pipeline, "execute_sql", rows)
    posts, _ = _install_http(
        monkeypatch,
        [_Response("SELECT id FROM invoices"), _Response("One row")],
        exit_callbacks=[None, stale_title_on_close],
    )
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await table_sql_pipeline.build_table_from_task("show one", limit=1)

    assert stopped.value.code == "budget_execution_inactive"
    assert len(posts) == 2
    physical = await _physical(factory, run["work_order_id"])
    assert len(physical) == 2
    assert {row.state for row in physical} == {"charged"}


@pytest.mark.asyncio
async def test_duplicate_context_cannot_replay_direct_helper(test_engine, monkeypatch):
    factory, run, first = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(monkeypatch, [_Response("first"), _Response("unsafe replay")])

    with bind_airouter_budget_context(first):
        assert (await ollama_client.generate("first")).text == "first"

    replay = WorkBudgetContext(
        work_order_id=first.work_order_id,
        step_id=first.step_id,
        attempt_id=first.attempt_id,
        session_factory=factory,
        expected_owner_key=first.expected_owner_key,
        expected_plan_id=first.expected_plan_id,
        expected_plan_revision=first.expected_plan_revision,
        expected_ledger_id=first.expected_ledger_id,
    )
    with bind_airouter_budget_context(replay):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await ollama_client.generate("replay")

    assert stopped.value.code == "llm_execution_already_started"
    assert len(posts) == 1
    assert len(await _physical(factory, run["work_order_id"])) == 1


@pytest.mark.asyncio
async def test_explicit_detached_and_ambient_collision_is_sticky(test_engine, monkeypatch):
    factory, _, ambient = await _claimed_context(test_engine)
    gpu_checks = _install_local_runtime(monkeypatch)
    posts, clients = _install_http(monkeypatch, [_Response('{"unsafe": true}')])

    async def current():
        return True

    detached = DetachedVerifierBudgetContext(
        work_order_id=ambient.work_order_id,
        owner_key=ambient.expected_owner_key,
        snapshot_digest="snapshot",
        operation_scope="collision",
        session_factory=factory,
        snapshot_is_current=current,
    )
    with bind_airouter_budget_context(ambient):
        with pytest.raises(BudgetExecutionStopped) as collision:
            await ollama_client.generate_json(
                "collision",
                model="direct-text-model",
                provider="ollama",
                budget_context=detached,
            )
        with pytest.raises(BudgetExecutionStopped) as repeated:
            await ollama_client.generate("still stopped")

    assert collision.value.code == "budget_context_collision"
    assert repeated.value is collision.value
    assert gpu_checks == []
    assert clients == []
    assert posts == []


class _OpenAIResponse(_Response):
    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


@pytest.mark.asyncio
async def test_strata_generate_json_charges_each_post_on_the_ledger(test_engine, monkeypatch):
    """With the GPU on Strata the verifier/planner/synthesize JSON went there.

    Live it was blocked as unsupported; Strata now runs through the same
    reserved-and-charged loop as Ollama, one reservation per physical POST.
    """
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch, provider="strata")
    posts, _ = _install_http(
        monkeypatch, [_OpenAIResponse("not-json"), _OpenAIResponse('{"ok": true}')]
    )

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with bind_airouter_budget_context(context):
        result = await ollama_client.generate_json(
            "json", model="qwen3.8-flash-next", provider="strata"
        )

    assert result == {"ok": True}
    physical = await _physical(factory, run["work_order_id"])
    assert len(posts) == len(physical) == 2
    assert {row.state for row in physical} == {"charged"}
    assert all(url.endswith("/v1/chat/completions") for url, _ in posts)
    assert all(kw["json"]["reasoning_effort"] == "none" for _, kw in posts)


@pytest.mark.asyncio
async def test_strata_reasoning_leaf_charges_one_post(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch, provider="strata")
    posts, _ = _install_http(monkeypatch, [_OpenAIResponse("reasoned")])

    with bind_airouter_budget_context(context):
        result = await ollama_client.reasoning_generate("reason", confidential=True)

    assert result == "reasoned"
    physical = await _physical(factory, run["work_order_id"])
    assert len(posts) == len(physical) == 1
    assert physical[0].state == "charged"


class _StrataStructuredOutputFailed:
    status_code = 502

    def raise_for_status(self):
        request = httpx.Request("POST", "http://strata:8080/v1/chat/completions")
        raise httpx.HTTPStatusError(
            "Server error '502 Bad Gateway'",
            request=request,
            response=httpx.Response(502, request=request),
        )

    def json(self):
        return {"error": {"type": "structured_output_failed", "code": "structured_output_failed"}}


@pytest.mark.asyncio
async def test_strata_invalid_json_answer_is_retried_like_ollama(test_engine, monkeypatch):
    """Strata answers 502 structured_output_failed when the model's json_object
    text is not JSON; the verifier stopped on it as a provider failure (live
    2026-10-06). It is the same retryable case as non-JSON Ollama content."""
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch, provider="strata")
    posts, _ = _install_http(
        monkeypatch, [_StrataStructuredOutputFailed(), _OpenAIResponse('{"ok": true}')]
    )

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with bind_airouter_budget_context(context):
        result = await ollama_client.generate_json(
            "json", model="qwen3.8-flash-next", provider="strata"
        )

    assert result == {"ok": True}
    physical = await _physical(factory, run["work_order_id"])
    assert len(posts) == len(physical) == 2
    assert {row.state for row in physical} == {"charged"}
