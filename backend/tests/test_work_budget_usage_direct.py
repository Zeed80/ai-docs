"""Provider usage receipts at the budgeted direct Ollama HTTP boundary."""

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.ai import ollama_client
from app.ai.planner_budget_context import DetachedPlannerBudgetContext
from app.ai.work_budget_context import (
    BudgetExecutionStopped,
    DetachedVerifierBudgetContext,
    bind_airouter_budget_context,
    bind_http_recipient_budget_context,
)
from app.auth.work_budget_handoff import resolve_sql_recipient_context
from app.db.models import WorkEvent, WorkOrder, WorkPlan
from app.domain.work_budget_usage import (
    LLM_USAGE_EVENT_TYPE,
    read_recorded_llm_usage,
)
from tests.test_work_budget_direct_text import (
    _call_leaf,
    _claimed_context,
    _install_http,
    _install_local_runtime,
)
from tests.test_work_budget_http_recipient import _request, _setup


class _UsageResponse:
    def __init__(
        self,
        content: str,
        *,
        input_tokens=3,
        output_tokens=5,
        done=True,
    ):
        self.body = {
            "done": done,
            "prompt_eval_count": input_tokens,
            "eval_count": output_tokens,
            "message": {"content": content},
        }

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class _HttpErrorResponse(_UsageResponse):
    def raise_for_status(self):
        request = httpx.Request("POST", "http://ollama.test/api/chat")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("unavailable", request=request, response=response)


class _InvalidBodyResponse(_UsageResponse):
    def json(self):
        raise ValueError("invalid response body")


async def _usage_events(factory, order_id):
    async with factory() as db:
        return list(
            await db.scalars(
                select(WorkEvent).where(
                    WorkEvent.work_order_id == order_id,
                    WorkEvent.event_type == LLM_USAGE_EVENT_TYPE,
                )
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["generate", "json", "chat", "reasoning"])
async def test_direct_leaf_records_terminal_top_level_counts(test_engine, monkeypatch, helper):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    content = '{"ok": true}' if helper == "json" else "complete"
    posts, _ = _install_http(
        monkeypatch,
        [_UsageResponse(content, input_tokens=7, output_tokens=11)],
    )

    with bind_airouter_budget_context(context):
        if helper == "reasoning":
            assert await ollama_client.reasoning_generate("reason") == "complete"
        else:
            await _call_leaf(helper)

    events = await _usage_events(factory, run["work_order_id"])
    assert len(posts) == len(events) == 1
    assert events[0].payload["tokens"] == {
        "input": {"status": "known", "units": 7},
        "output": {"status": "known", "units": 11},
        "total": {"status": "known", "units": 18},
    }
    assert events[0].payload["cost"] == {
        "status": "unknown",
        "currency": "USD",
        "reason": "tariff_unavailable",
        "tariff_version": None,
    }


@pytest.mark.asyncio
async def test_generate_retry_records_unknown_then_known_without_double_count(
    test_engine, monkeypatch
):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(
        monkeypatch,
        [
            httpx.ConnectError("offline", request=httpx.Request("POST", "http://ollama.test")),
            _UsageResponse("ready", input_tokens=2, output_tokens=4),
        ],
    )

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(ollama_client, "_async_sleep", no_sleep)
    with bind_airouter_budget_context(context):
        result = await ollama_client.generate("retry", max_retries=1)

    assert result.text == "ready"
    summary = await read_recorded_llm_usage(
        factory,
        work_order_id=run["work_order_id"],
        expected_owner_key=context.expected_owner_key,
    )
    assert len(posts) == 2
    assert summary["attempts"] == {
        "recorded": 2,
        "known_total": 1,
        "unknown": 1,
        "missing_receipts": 0,
        "invalid_receipts": 0,
        "reserved": 0,
        "unsupported_reservations": 0,
    }
    assert summary["tokens"] == {
        "status": "partial",
        "input_known_lower_bound": 2,
        "output_known_lower_bound": 4,
        "known_lower_bound": 6,
        "total": None,
    }


@pytest.mark.asyncio
async def test_invalid_generated_json_retries_with_two_known_receipts(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(
        monkeypatch,
        [
            _UsageResponse("not-json", input_tokens=3, output_tokens=4),
            _UsageResponse('{"ok": true}', input_tokens=5, output_tokens=6),
        ],
    )

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with bind_airouter_budget_context(context):
        assert await ollama_client.generate_json(
            "json", model="direct-text-model", provider="ollama"
        ) == {"ok": True}

    events = await _usage_events(factory, run["work_order_id"])
    assert len(posts) == len(events) == 2
    assert sorted(event.payload["tokens"]["total"]["units"] for event in events) == [7, 11]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (_HttpErrorResponse("ignored"), "http_error"),
        (_InvalidBodyResponse("ignored"), "response_body_invalid"),
    ],
)
async def test_unusable_http_response_records_explicit_unknown_receipt(
    test_engine, monkeypatch, response, outcome
):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    _install_http(monkeypatch, [response])

    with bind_airouter_budget_context(context):
        with pytest.raises(Exception):
            await ollama_client.generate("unusable", max_retries=0)

    events = await _usage_events(factory, run["work_order_id"])
    assert len(events) == 1
    assert events[0].payload["outcome"] == outcome
    assert events[0].payload["tokens"] == {
        "input": {"status": "unknown", "reason": outcome},
        "output": {"status": "unknown", "reason": outcome},
        "total": {"status": "unknown", "reason": "terminal_not_observed"},
    }


@pytest.mark.asyncio
async def test_nonterminal_partial_counts_are_only_a_known_lower_bound(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    response = _UsageResponse("partial", input_tokens=9, done=False)
    response.body.pop("eval_count")
    _install_http(monkeypatch, [response])

    with bind_airouter_budget_context(context):
        await ollama_client.generate("partial", max_retries=0)

    summary = await read_recorded_llm_usage(
        factory,
        work_order_id=run["work_order_id"],
        expected_owner_key=context.expected_owner_key,
    )
    assert summary["attempts"]["known_total"] == 0
    assert summary["attempts"]["unknown"] == 1
    assert summary["tokens"]["input_known_lower_bound"] == 9
    assert summary["tokens"]["output_known_lower_bound"] == 0
    assert summary["tokens"]["total"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["generate", "json", "chat"])
@pytest.mark.parametrize("race", ["stale", "close_error"])
async def test_usage_observed_before_client_exit_is_persisted(
    test_engine, monkeypatch, helper, race
):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)

    async def exit_callback():
        if race == "close_error":
            raise RuntimeError("client close failed")
        async with factory() as db:
            order = await db.get(WorkOrder, run["work_order_id"])
            plan = await db.get(WorkPlan, context.expected_plan_id)
            order.plan_revision += 1
            plan.status = "superseded"
            await db.commit()

    content = '{"ok": true}' if helper == "json" else "complete"
    posts, _ = _install_http(
        monkeypatch,
        [_UsageResponse(content, input_tokens=5, output_tokens=6)],
        exit_callbacks=[exit_callback],
    )

    with bind_airouter_budget_context(context):
        if race == "stale":
            with pytest.raises(BudgetExecutionStopped) as stopped:
                await _call_leaf(helper)
            assert stopped.value.code == "budget_execution_inactive"
        else:
            with pytest.raises(Exception, match="client close failed"):
                await _call_leaf(helper)

    events = await _usage_events(factory, run["work_order_id"])
    assert len(posts) == len(events) == 1
    assert events[0].payload["tokens"]["total"] == {"status": "known", "units": 11}


@pytest.mark.asyncio
async def test_usage_observed_before_cancelled_client_exit_is_persisted(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)

    async def cancel_on_exit():
        raise asyncio.CancelledError()

    _install_http(
        monkeypatch,
        [_UsageResponse("complete", input_tokens=6, output_tokens=7)],
        exit_callbacks=[cancel_on_exit],
    )
    with bind_airouter_budget_context(context):
        with pytest.raises(asyncio.CancelledError):
            await ollama_client.generate("cancel-on-close", max_retries=0)

    events = await _usage_events(factory, run["work_order_id"])
    assert len(events) == 1
    assert events[0].payload["tokens"]["total"] == {"status": "known", "units": 13}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["planner", "verifier"])
async def test_explicit_detached_context_records_usage(test_engine, monkeypatch, kind):
    factory, run, ambient = await _claimed_context(test_engine)
    _install_local_runtime(monkeypatch)
    _install_http(monkeypatch, [_UsageResponse('{"ok": true}', input_tokens=1, output_tokens=2)])

    async def current():
        return True

    common = {
        "work_order_id": ambient.work_order_id,
        "owner_key": ambient.expected_owner_key,
        "snapshot_digest": "1" * 64,
        "operation_scope": f"usage-{kind}",
        "session_factory": factory,
        "snapshot_is_current": current,
    }
    if kind == "planner":
        context = DetachedPlannerBudgetContext(prompt_digest="2" * 64, **common)
    else:
        context = DetachedVerifierBudgetContext(**common)

    assert await ollama_client.generate_json(
        "explicit",
        model="direct-text-model",
        provider="ollama",
        budget_context=context,
    ) == {"ok": True}
    events = await _usage_events(factory, run["work_order_id"])
    assert len(events) == 1
    assert events[0].payload["tokens"]["total"]["units"] == 3


@pytest.mark.asyncio
async def test_ambient_http_recipient_records_usage(test_engine, monkeypatch):
    factory, run, parent, headers = await _setup(test_engine, monkeypatch)
    context = await resolve_sql_recipient_context(_request(headers))
    _install_local_runtime(monkeypatch)
    _install_http(monkeypatch, [_UsageResponse("recipient", input_tokens=4, output_tokens=8)])

    with bind_http_recipient_budget_context(context):
        result = await ollama_client.chat([{"role": "user", "content": "recipient"}])

    assert result.text == "recipient"
    summary = await read_recorded_llm_usage(
        factory,
        work_order_id=run["work_order_id"],
        expected_owner_key=parent.expected_owner_key,
    )
    assert summary["attempts"]["recorded"] == 1
    assert summary["attempts"]["known_total"] == 1
    assert summary["tokens"]["total"] == 12
