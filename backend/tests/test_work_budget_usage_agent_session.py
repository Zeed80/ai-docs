"""E21.2b11: usage receipts for durable streaming AgentSession Ollama attempts."""

import asyncio
import json

import httpx
import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.work_budget_context import BudgetExecutionStopped
from tests.test_work_budget_airouter import _claimed_context
from tests.test_work_budget_usage_airouter import _receipts, _summary


def _config(provider="ollama", *, fallback=None) -> BuiltinAgentConfig:
    config = BuiltinAgentConfig(department_enabled=False, provider=provider)
    if fallback:
        # Test-only mutation of the retired fallback chain (see
        # test_work_budget_provider) to prove each attempt is accounted.
        config.fallback_providers = [fallback]
    return config


def _chunks(content="ok", *, input_tokens=3, output_tokens=5, done=True):
    lines = [json.dumps({"message": {"content": content}, "done": False})]
    if done:
        final = {"message": {"content": ""}, "done": True}
        if input_tokens is not None:
            final["prompt_eval_count"] = input_tokens
        if output_tokens is not None:
            final["eval_count"] = output_tokens
        lines.append(json.dumps(final))
    return lines


class _StreamResponse:
    def __init__(self, lines, *, status=200):
        self.lines = lines
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            request = httpx.Request("POST", "http://ollama.test/api/chat")
            response = httpx.Response(self.status, request=request)
            raise httpx.HTTPStatusError("unavailable", request=request, response=response)

    async def aiter_lines(self):
        for line in self.lines:
            if isinstance(line, BaseException):
                raise line
            yield line


def _install_stream(monkeypatch, outcomes):
    posts = []

    class _Stream:
        def __init__(self, outcome):
            self.outcome = outcome

        async def __aenter__(self):
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome

        async def __aexit__(self, *_args):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url, **kwargs):
            posts.append(url)
            return _Stream(outcomes.pop(0))

    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(agent_loop, "_get_agent_model", lambda *_a, **_k: "budget-model")
    monkeypatch.setattr(agent_loop, "_thinking_disabled", lambda *_a, **_k: True)
    return posts


async def _dispatch(context, config, tokens=None):
    async def on_token(token):
        if tokens is not None:
            tokens.append(token)

    return await agent_loop._call_provider_streaming(
        [{"role": "user", "content": "test"}],
        [],
        None,
        config,
        on_token,
        budget_context=context,
    )


@pytest.mark.asyncio
async def test_streamed_final_chunk_records_known_receipt(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    posts = _install_stream(monkeypatch, [_StreamResponse(_chunks(input_tokens=17))])

    result = await _dispatch(context, _config())

    assert result["content"] == "ok"
    assert result["_usage"] == {"input_tokens": 17, "output_tokens": 5}
    reservations, [event] = await _receipts(factory, run["work_order_id"])
    assert len(posts) == len(reservations) == 1
    assert reservations[0].state == "charged"
    assert event.payload["tokens"]["total"] == {"status": "known", "units": 22}
    summary = await _summary(factory, run["work_order_id"])
    assert summary["tokens"]["status"] == "known"
    assert summary["tokens"]["total"] == 22


@pytest.mark.asyncio
async def test_malformed_counters_keep_answer_and_stay_unknown(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_stream(monkeypatch, [_StreamResponse(_chunks(input_tokens="abc", output_tokens=True))])

    result = await _dispatch(context, _config())

    # Previously int("abc") crashed and dropped the streamed answer.
    assert result["content"] == "ok"
    assert result["_usage"] == {"input_tokens": None, "output_tokens": None}
    _, [event] = await _receipts(factory, run["work_order_id"])
    assert event.payload["tokens"]["input"] == {"status": "unknown", "reason": "invalid_type"}
    assert event.payload["tokens"]["total"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_stream_without_done_records_unknown_receipt(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_stream(monkeypatch, [_StreamResponse(_chunks(done=False))])

    await _dispatch(context, _config())

    _, [event] = await _receipts(factory, run["work_order_id"])
    assert event.payload["outcome"] == "response_not_observed"
    summary = await _summary(factory, run["work_order_id"])
    assert summary["tokens"]["status"] == "unknown"
    assert summary["tokens"]["total"] is None


@pytest.mark.asyncio
async def test_transient_retry_records_each_attempt(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(agent_loop.asyncio, "sleep", no_sleep)
    posts = _install_stream(
        monkeypatch,
        [
            _StreamResponse([_chunks()[0], httpx.ReadError("stream cut")]),
            _StreamResponse(_chunks(input_tokens=4, output_tokens=6)),
        ],
    )

    result = await _dispatch(context, _config())

    assert result["content"] == "ok"
    reservations, events = await _receipts(factory, run["work_order_id"])
    assert len(posts) == len(reservations) == len(events) == 2
    assert {row.state for row in reservations} == {"charged"}
    assert [event.payload["outcome"] for event in events] == [
        "response_not_observed",
        "response_observed",
    ]
    summary = await _summary(factory, run["work_order_id"])
    assert summary["tokens"]["status"] == "partial"
    assert summary["tokens"]["known_lower_bound"] == 10
    assert summary["tokens"]["total"] is None


@pytest.mark.asyncio
async def test_http_error_is_charged_with_http_error_receipt(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_stream(monkeypatch, [_StreamResponse([], status=503)])

    with pytest.raises(httpx.HTTPStatusError):
        await _dispatch(context, _config())

    reservations, [event] = await _receipts(factory, run["work_order_id"])
    assert reservations[0].state == "charged"
    assert event.payload["outcome"] == "http_error"


@pytest.mark.asyncio
async def test_cancellation_mid_stream_charges_unknown_receipt(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_stream(monkeypatch, [_StreamResponse([_chunks()[0], asyncio.CancelledError()])])

    with pytest.raises(asyncio.CancelledError):
        await _dispatch(context, _config())

    reservations, [event] = await _receipts(factory, run["work_order_id"])
    assert reservations[0].state == "charged"
    assert event.payload["outcome"] == "response_not_observed"


@pytest.mark.asyncio
async def test_receipt_settlement_failure_is_sticky_without_fallback(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    _install_stream(monkeypatch, [_StreamResponse(_chunks())])
    fallback_calls = []

    async def fallback(*_args, **_kwargs):
        fallback_calls.append("openai")
        return {"content": "unsafe"}

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("settlement unavailable")

    monkeypatch.setattr(agent_loop, "_call_openai_streaming", fallback)
    monkeypatch.setattr(
        "app.ai.work_budget_context.settle_llm_call_with_usage_receipt", unavailable
    )
    with pytest.raises(BudgetExecutionStopped) as stopped:
        await _dispatch(context, _config(fallback="openai"))

    assert stopped.value.code == "llm_budget_settlement_unavailable"
    assert fallback_calls == []
    reservations, events = await _receipts(factory, run["work_order_id"])
    assert [row.state for row in reservations] == ["reserved"]
    assert events == [None]


@pytest.mark.asyncio
async def test_non_ollama_provider_keeps_missing_receipt(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)

    async def openai(*_args, **_kwargs):
        return {"content": "cloud"}

    monkeypatch.setattr(agent_loop, "_call_openai_streaming", openai)
    await _dispatch(context, _config(provider="openai"))

    reservations, events = await _receipts(factory, run["work_order_id"])
    assert [row.state for row in reservations] == ["charged"]
    assert events == [None]
    summary = await _summary(factory, run["work_order_id"])
    assert summary["attempts"]["missing_receipts"] == 1
    assert summary["tokens"]["status"] == "unknown"
