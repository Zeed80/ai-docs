"""E21.2b10: usage receipts for durable AIRouter Ollama attempts."""

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.ai.providers import ollama as ollama_provider
from app.ai.providers.ollama import OllamaProvider
from app.ai.schemas import ProviderConfig, ProviderKind
from app.ai.work_budget_context import BudgetExecutionStopped, bind_airouter_budget_context
from app.db.models import WorkEvent, WorkOrder, WorkPlan
from app.domain import work_budget_usage
from app.domain.work_budget_usage import (
    LLM_USAGE_EVENT_TYPE,
    capture_ollama_usage,
    observe_ollama_response,
    read_recorded_llm_usage,
    usage_event_id,
)
from tests.test_work_budget_airouter import (
    Decision,
    SequencedProvider,
    _async_none,
    _claimed_context,
    _decision_request,
    _physical,
    _router,
)


def _body(content, *, input_tokens=3, output_tokens=5, done=True):
    body = {"message": {"content": content}}
    if done is not None:
        body["done"] = done
    if input_tokens is not None:
        body["prompt_eval_count"] = input_tokens
    if output_tokens is not None:
        body["eval_count"] = output_tokens
    return body


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class _HttpErrorResponse(_Response):
    def raise_for_status(self):
        request = httpx.Request("POST", "http://provider.test/api/chat")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("unavailable", request=request, response=response)


class _InvalidBodyResponse(_Response):
    def json(self):
        raise ValueError("invalid response body")


def _install_ollama(monkeypatch, outcomes):
    """Serve scripted responses to the real OllamaProvider HTTP boundary."""
    posts = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            posts.append(url)
            outcome = outcomes.pop(0)
            if callable(outcome):
                outcome = await outcome()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome if isinstance(outcome, _Response) else _Response(outcome)

    monkeypatch.setattr(ollama_provider.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr("app.ai.server_lifecycle.ensure_running", lambda _kind: _async_none())
    return posts


def _ollama_router(monkeypatch):
    provider = OllamaProvider(
        ProviderConfig(kind=ProviderKind.OLLAMA, base_url="http://provider.test")
    )
    return _router(monkeypatch, provider)


async def _receipts(factory, order_id):
    reservations = sorted(
        await _physical(factory, order_id), key=lambda row: (row.reserved_at, row.id)
    )
    async with factory() as db:
        events = {
            event.id: event
            for event in await db.scalars(
                select(WorkEvent).where(
                    WorkEvent.work_order_id == order_id,
                    WorkEvent.event_type == LLM_USAGE_EVENT_TYPE,
                )
            )
        }
    return reservations, [events.get(usage_event_id(row.id)) for row in reservations]


async def _summary(factory, order_id):
    async with factory() as db:
        owner_key = (await db.get(WorkOrder, order_id)).owner_key
    return await read_recorded_llm_usage(
        factory, work_order_id=order_id, expected_owner_key=owner_key
    )


@pytest.mark.asyncio
async def test_format_retry_records_one_known_receipt_per_physical_post(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    router = _ollama_router(monkeypatch)
    posts = _install_ollama(
        monkeypatch,
        [
            _body("not-json", input_tokens=11, output_tokens=2),
            _body('{"ok": true}', input_tokens=13, output_tokens=4),
        ],
    )

    with bind_airouter_budget_context(context):
        response = await router.run(_decision_request())

    assert response.data == Decision(ok=True)
    reservations, events = await _receipts(factory, run["work_order_id"])
    assert len(posts) == len(reservations) == len(events) == 2
    assert {row.state for row in reservations} == {"charged"}
    assert [event.payload["tokens"]["total"] for event in events] == [
        {"status": "known", "units": 13},
        {"status": "known", "units": 17},
    ]
    assert {event.payload["cost"]["status"] for event in events} == {"unknown"}
    summary = await _summary(factory, run["work_order_id"])
    assert summary["coverage"]["complete_for_scope"] is True
    assert summary["tokens"]["status"] == "known"
    assert summary["tokens"]["total"] == 30
    assert summary["cost"]["total"] is None


@pytest.mark.asyncio
async def test_raw_counts_are_strict_even_though_airesponse_coerces(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    router = _ollama_router(monkeypatch)
    _install_ollama(monkeypatch, [_body('{"ok": true}', input_tokens="3", output_tokens=True)])

    with bind_airouter_budget_context(context):
        response = await router.run(_decision_request())

    # A malformed counter neither discards the valid answer nor looks measured.
    assert response.data == Decision(ok=True)
    assert response.usage.input_tokens is None
    assert response.usage.output_tokens is None
    assert response.usage.total_tokens is None
    _, [event] = await _receipts(factory, run["work_order_id"])
    assert event.payload["tokens"] == {
        "input": {"status": "unknown", "reason": "invalid_type"},
        "output": {"status": "unknown", "reason": "invalid_type"},
        "total": {"status": "unknown", "reason": "component_unknown"},
    }
    summary = await _summary(factory, run["work_order_id"])
    assert summary["tokens"]["status"] == "unknown"
    assert summary["tokens"]["known_lower_bound"] == 0
    assert summary["tokens"]["total"] is None


@pytest.mark.asyncio
async def test_missing_done_keeps_counts_as_lower_bound_only(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    router = _ollama_router(monkeypatch)
    _install_ollama(monkeypatch, [_body('{"ok": true}', done=None)])

    with bind_airouter_budget_context(context):
        await router.run(_decision_request())

    _, [event] = await _receipts(factory, run["work_order_id"])
    assert event.payload["terminal_observed"] is False
    assert event.payload["tokens"]["total"] == {
        "status": "unknown",
        "reason": "terminal_not_observed",
    }
    summary = await _summary(factory, run["work_order_id"])
    assert summary["tokens"]["status"] == "partial"
    assert summary["tokens"]["known_lower_bound"] == 8
    assert summary["tokens"]["total"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (_HttpErrorResponse({}), "http_error"),
        (_InvalidBodyResponse({}), "response_body_invalid"),
        (httpx.ConnectTimeout("timeout"), "response_not_observed"),
    ],
)
async def test_failed_post_is_charged_with_explicit_unknown_receipt(
    test_engine, monkeypatch, outcome, reason
):
    factory, run, context = await _claimed_context(test_engine)
    router = _ollama_router(monkeypatch)
    posts = _install_ollama(monkeypatch, [outcome])

    with bind_airouter_budget_context(context):
        with pytest.raises(Exception) as raised:
            await router.run(_decision_request())

    assert not isinstance(raised.value, BudgetExecutionStopped)
    reservations, [event] = await _receipts(factory, run["work_order_id"])
    assert len(posts) == len(reservations) == 1
    assert reservations[0].state == "charged"
    assert event.payload["outcome"] == reason
    assert event.payload["tokens"]["input"] == {"status": "unknown", "reason": reason}
    summary = await _summary(factory, run["work_order_id"])
    assert summary["attempts"]["unknown"] == 1
    assert summary["tokens"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_stale_postflight_keeps_observed_usage_and_blocks_response(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    router = _ollama_router(monkeypatch)

    async def stale_then_answer():
        async with factory() as db:
            order = await db.get(WorkOrder, run["work_order_id"])
            plan = await db.get(WorkPlan, context.expected_plan_id)
            plan.status = "superseded"
            order.plan_revision += 1
            await db.commit()
        return _body('{"ok": true}', input_tokens=21, output_tokens=1)

    posts = _install_ollama(monkeypatch, [stale_then_answer])
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await router.run(_decision_request())

    assert stopped.value.code == "budget_execution_inactive"
    assert len(posts) == 1
    _, [event] = await _receipts(factory, run["work_order_id"])
    assert event.payload["tokens"]["total"] == {"status": "known", "units": 22}


@pytest.mark.asyncio
async def test_cancellation_during_post_charges_unknown_receipt(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    router = _ollama_router(monkeypatch)
    _install_ollama(monkeypatch, [asyncio.CancelledError()])

    with bind_airouter_budget_context(context):
        with pytest.raises(asyncio.CancelledError):
            await router.run(_decision_request())

    reservations, [event] = await _receipts(factory, run["work_order_id"])
    assert reservations[0].state == "charged"
    assert event.payload["outcome"] == "response_not_observed"


@pytest.mark.asyncio
async def test_settlement_failure_is_sticky_and_reservation_stays_reserved(
    test_engine, monkeypatch
):
    factory, run, context = await _claimed_context(test_engine)
    router = _ollama_router(monkeypatch)
    posts = _install_ollama(monkeypatch, [_body("not-json"), _body('{"ok": true}')])

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("settlement unavailable")

    monkeypatch.setattr(
        "app.ai.work_budget_context.settle_llm_call_with_usage_receipt", unavailable
    )
    with bind_airouter_budget_context(context):
        with pytest.raises(BudgetExecutionStopped) as stopped:
            await router.run(_decision_request())

    assert stopped.value.code == "llm_budget_settlement_unavailable"
    assert len(posts) == 1
    reservations, events = await _receipts(factory, run["work_order_id"])
    assert [row.state for row in reservations] == ["reserved"]
    assert events == [None]
    summary = await _summary(factory, run["work_order_id"])
    assert summary["attempts"]["missing_receipts"] == 1
    assert summary["tokens"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_provider_without_http_boundary_records_unknown_not_zero(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    router = _router(monkeypatch, SequencedProvider(['{"ok": true}']))
    monkeypatch.setattr("app.ai.server_lifecycle.ensure_running", lambda _kind: _async_none())

    with bind_airouter_budget_context(context):
        await router.run(_decision_request())

    _, [event] = await _receipts(factory, run["work_order_id"])
    assert event.payload["outcome"] == "response_not_observed"
    summary = await _summary(factory, run["work_order_id"])
    assert summary["tokens"]["status"] == "unknown"
    assert summary["tokens"]["total"] is None


@pytest.mark.asyncio
async def test_capture_does_not_leak_outside_budgeted_dispatch(test_engine, monkeypatch):
    _, _, context = await _claimed_context(test_engine)
    router = _ollama_router(monkeypatch)
    _install_ollama(monkeypatch, [_body('{"ok": true}')])

    with bind_airouter_budget_context(context):
        await router.run(_decision_request())

    assert work_budget_usage._OLLAMA_USAGE_CAPTURE.get() is None


def test_observe_without_capture_keeps_plain_provider_semantics():
    assert observe_ollama_response(_Response({"done": True})) == {"done": True}
    with pytest.raises(httpx.HTTPStatusError):
        observe_ollama_response(_HttpErrorResponse({}))


def test_second_response_under_one_capture_degrades_to_unknown():
    with capture_ollama_usage() as capture:
        observe_ollama_response(_Response(_body("a")))
        assert capture.evidence.input_tokens.units == 3
        observe_ollama_response(_Response(_body("b")))
    assert capture.observations == 2
    assert capture.evidence.terminal_observed is False
    assert capture.evidence.input_tokens.units is None
