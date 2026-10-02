"""E21.2a provider dispatch is fenced by the durable shared ledger."""

import asyncio
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.work_budget_context import BudgetExecutionStopped, WorkBudgetContext
from app.api.chat_runs import ChatRunCreate, submit_chat_run
from app.auth.jwt import _DEV_USER
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.work_orders import claim_ready_step


def _request() -> ChatRunCreate:
    return ChatRunCreate(request_id=uuid.uuid4(), content="Budget provider test")


async def _claimed_context(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        run = await submit_chat_run(_request(), db, _DEV_USER)
    async with factory() as db:
        _, step, attempt = await claim_ready_step(
            db, worker_id="budget-provider-test", work_order_id=run["work_order_id"]
        )
        await db.commit()
    return (
        factory,
        run,
        WorkBudgetContext(
            work_order_id=run["work_order_id"],
            step_id=step.id,
            attempt_id=attempt.id,
            session_factory=factory,
        ),
    )


def _config(*, fallback: bool = False) -> BuiltinAgentConfig:
    config = BuiltinAgentConfig(department_enabled=False, provider="ollama")
    if fallback:
        # The production validator retires implicit fallback configuration.
        # This explicit test-only mutation exercises the still-supported
        # dispatcher chain and proves that every physical attempt is charged.
        config.fallback_providers = ["openai"]
    return config


async def _dispatch(context, config):
    return await agent_loop._call_provider_streaming(
        [{"role": "user", "content": "test"}],
        [],
        None,
        config,
        lambda _token: asyncio.sleep(0),
        budget_context=context,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transient, expected_calls", [(False, 2), (True, 3)])
async def test_retry_and_fallback_each_consume_a_physical_call(
    test_engine, monkeypatch, transient, expected_calls
):
    factory, run, context = await _claimed_context(test_engine)
    await context.assert_ready()
    calls = []

    async def ollama(*args, **kwargs):
        calls.append("ollama")
        if transient:
            raise httpx.ReadError(
                "stream ended", request=httpx.Request("POST", "http://provider.test")
            )
        raise RuntimeError("primary failed")

    async def openai(*args, **kwargs):
        calls.append("openai")
        return {"role": "assistant", "content": "fallback"}

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(agent_loop, "_call_ollama_streaming", ollama)
    monkeypatch.setattr(agent_loop, "_call_openai_streaming", openai)
    monkeypatch.setattr(agent_loop.asyncio, "sleep", no_sleep)

    assert (await _dispatch(context, _config(fallback=True)))["content"] == "fallback"
    assert len(calls) == expected_calls
    async with factory() as db:
        reservations = list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.work_order_id == run["work_order_id"]
                )
            )
        )
    physical = [row for row in reservations if row.operation_key.startswith("llm:")]
    markers = [row for row in reservations if row.operation_key.startswith("execution:")]
    assert len(physical) == expected_calls
    assert {row.state for row in physical} == {"charged"}
    assert len(markers) == 1
    assert markers[0].reserved_units == 0


@pytest.mark.asyncio
async def test_reservation_database_failure_is_sticky_and_never_falls_back(
    test_engine, monkeypatch
):
    _, _, context = await _claimed_context(test_engine)
    await context.assert_ready()
    provider_calls = []

    async def broken_reserve(*args, **kwargs):
        raise RuntimeError("database offline")

    async def provider(*args, **kwargs):
        provider_calls.append("called")
        return {"content": "must not run"}

    monkeypatch.setattr("app.ai.work_budget_context.reserve_budget_for_dispatch", broken_reserve)
    monkeypatch.setattr(agent_loop, "_call_ollama_streaming", provider)
    monkeypatch.setattr(agent_loop, "_call_openai_streaming", provider)

    with pytest.raises(BudgetExecutionStopped) as first:
        await _dispatch(context, _config(fallback=True))
    assert first.value.code == "llm_budget_reservation_unavailable"
    with pytest.raises(BudgetExecutionStopped) as repeated:
        await _dispatch(context, _config(fallback=True))
    assert repeated.value is first.value
    assert provider_calls == []


@pytest.mark.asyncio
async def test_settlement_database_failure_stops_without_fallback_and_keeps_reserve(
    test_engine, monkeypatch
):
    factory, run, context = await _claimed_context(test_engine)
    await context.assert_ready()
    calls = []

    async def provider(*args, **kwargs):
        calls.append("ollama")
        return {"content": "dispatched"}

    async def fallback(*args, **kwargs):
        calls.append("fallback")
        return {"content": "unsafe"}

    async def broken_settle(*args, **kwargs):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(agent_loop, "_call_ollama_streaming", provider)
    monkeypatch.setattr(agent_loop, "_call_openai_streaming", fallback)
    monkeypatch.setattr("app.ai.work_budget_context.settle_budget", broken_settle)

    with pytest.raises(BudgetExecutionStopped) as stopped:
        await _dispatch(context, _config(fallback=True))
    assert stopped.value.code == "llm_budget_settlement_unavailable"
    assert calls == ["ollama"]
    async with factory() as db:
        physical = list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.work_order_id == run["work_order_id"],
                    WorkBudgetReservation.operation_key.like("llm:%"),
                )
            )
        )
    assert len(physical) == 1
    assert physical[0].state == "reserved"
    assert physical[0].reserved_units == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("max_tokens", Decimal("100"), "token_budget_enforcement_unavailable"),
        ("max_cost_usd", Decimal("1.25"), "cost_budget_enforcement_unavailable"),
    ],
)
async def test_finite_unprovable_caps_stop_before_provider(
    test_engine, monkeypatch, field, value, code
):
    factory, run, context = await _claimed_context(test_engine)
    async with factory() as db:
        ledger = await db.scalar(
            select(WorkBudgetLedger)
            .join(
                WorkBudgetReservation,
                WorkBudgetReservation.ledger_id == WorkBudgetLedger.id,
                isouter=True,
            )
            .where(WorkBudgetLedger.root_work_order_id == run["work_order_id"])
        )
        setattr(ledger, field, value)
        await db.commit()
    await context.assert_ready()
    provider_calls = []

    async def provider(*args, **kwargs):
        provider_calls.append("called")
        return {"content": "must not run"}

    monkeypatch.setattr(agent_loop, "_call_ollama_streaming", provider)
    with pytest.raises(BudgetExecutionStopped) as stopped:
        await _dispatch(context, _config())
    assert stopped.value.code == code
    assert provider_calls == []


@pytest.mark.asyncio
async def test_duplicate_contexts_with_different_providers_dispatch_only_once(
    test_engine, monkeypatch
):
    factory, run, first = await _claimed_context(test_engine)
    second = WorkBudgetContext(
        work_order_id=first.work_order_id,
        step_id=first.step_id,
        attempt_id=first.attempt_id,
        session_factory=factory,
    )
    provider_calls = []

    async def provider(*args, **kwargs):
        provider_calls.append(kwargs.get("provider", "ollama"))
        await asyncio.sleep(0.05)
        return {"content": "one result"}

    monkeypatch.setattr(agent_loop, "_call_ollama_streaming", provider)
    monkeypatch.setattr(agent_loop, "_call_openai_streaming", provider)
    results = await asyncio.gather(
        _dispatch(first, _config()),
        _dispatch(
            second,
            BuiltinAgentConfig(department_enabled=False, provider="openai"),
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    failures = [result for result in results if isinstance(result, BudgetExecutionStopped)]
    assert len(failures) == 1
    assert failures[0].code == "llm_execution_already_started"
    assert len(provider_calls) == 1
    async with factory() as db:
        markers = list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.work_order_id == run["work_order_id"],
                    WorkBudgetReservation.operation_key == f"execution:{first.attempt_id}",
                )
            )
        )
    assert len(markers) == 1
    assert markers[0].reserved_units == 0


@pytest.mark.asyncio
async def test_provider_crash_consumes_call_and_same_attempt_cannot_replay(
    test_engine, monkeypatch
):
    factory, run, first = await _claimed_context(test_engine)
    provider_calls = []

    class ProviderCrash(BaseException):
        pass

    async def crash(*args, **kwargs):
        provider_calls.append("called")
        raise ProviderCrash("process lost after dispatch")

    monkeypatch.setattr(agent_loop, "_call_ollama_streaming", crash)
    with pytest.raises(ProviderCrash):
        await _dispatch(first, _config())

    replay = WorkBudgetContext(
        work_order_id=first.work_order_id,
        step_id=first.step_id,
        attempt_id=first.attempt_id,
        session_factory=factory,
    )
    with pytest.raises(BudgetExecutionStopped) as stopped:
        await _dispatch(replay, _config())
    assert stopped.value.code == "llm_execution_already_started"
    assert provider_calls == ["called"]
    async with factory() as db:
        physical = list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.work_order_id == run["work_order_id"],
                    WorkBudgetReservation.operation_key.like("llm:%"),
                )
            )
        )
    assert len(physical) == 1
    assert physical[0].state == "charged"
