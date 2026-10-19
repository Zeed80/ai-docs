"""The semantic verifier yields Strata to people's turns.

Strata serves two requests at once; a verdict running beside a chat turn
halves that turn's speed, while nobody waits for the verdict itself.
"""

from __future__ import annotations

import time
import uuid

import pytest

from app.ai.providers import strata_manager
from app.tasks import work_orders


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _metrics(monkeypatch, payload):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, headers=None):
            if isinstance(payload, Exception):
                raise payload
            return _Response(payload)

    monkeypatch.setattr(strata_manager.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(strata_manager, "_base_url", lambda: "http://strata")


@pytest.mark.asyncio
async def test_strata_is_busy_while_it_serves_a_request(monkeypatch):
    _metrics(monkeypatch, {"live": {"running": 1}, "requests": []})
    assert await strata_manager.busy() is True


@pytest.mark.asyncio
async def test_strata_is_busy_right_after_a_request(monkeypatch):
    _metrics(monkeypatch, {"live": {"running": 0}, "requests": [{"time": time.time() - 1}]})
    assert await strata_manager.busy() is True


@pytest.mark.asyncio
async def test_a_long_request_counts_from_its_end(monkeypatch):
    # Live 2026-10-19: a 6 s call started 7 s ago had ended a second before.
    _metrics(
        monkeypatch,
        {"live": {"running": 0}, "requests": [{"time": time.time() - 7, "duration_s": 6.3}]},
    )
    assert await strata_manager.busy() is True


@pytest.mark.asyncio
async def test_strata_is_free_after_a_quiet_spell(monkeypatch):
    _metrics(monkeypatch, {"live": {"running": 0}, "requests": [{"time": time.time() - 60}]})
    assert await strata_manager.busy() is False


@pytest.mark.asyncio
async def test_unreadable_metrics_do_not_hold_the_verifier(monkeypatch):
    _metrics(monkeypatch, RuntimeError("down"))
    assert await strata_manager.busy() is False


@pytest.fixture
def scheduled(monkeypatch):
    calls = {"later": [], "learn": [], "verified": []}

    class _Task:
        def __init__(self, name):
            self.name = name

        def apply_async(self, args, **kwargs):
            calls[self.name].append((args, kwargs))

    monkeypatch.setattr(work_orders, "verify_order_semantics", _Task("later"))
    monkeypatch.setattr(work_orders, "learn_work_order", _Task("learn"))

    async def _verify(order_id, **kwargs):
        calls["verified"].append(order_id)
        return True

    monkeypatch.setattr(work_orders, "verify_semantic_criteria", _verify)
    return calls


def _gpu(monkeypatch, busy: bool):
    async def _wait():
        return busy

    monkeypatch.setattr(work_orders, "_verifier_should_wait", _wait)


@pytest.mark.asyncio
async def test_a_busy_gpu_puts_the_verdict_off(monkeypatch, scheduled):
    _gpu(monkeypatch, True)
    order_id = uuid.uuid4()

    assert await work_orders.verify_order_semantics_later(order_id, 3) is False

    assert scheduled["verified"] == []
    ((args, kwargs),) = scheduled["later"]
    assert args == [str(order_id), 4]
    assert kwargs["countdown"] == work_orders._VERIFY_DEFER_SECONDS


@pytest.mark.asyncio
async def test_a_quiet_gpu_gets_the_verdict_and_the_learning(monkeypatch, scheduled):
    _gpu(monkeypatch, False)
    order_id = uuid.uuid4()

    assert await work_orders.verify_order_semantics_later(order_id, 2) is True

    assert scheduled["verified"] == [order_id]
    assert scheduled["learn"][0][0] == [str(order_id)]
    assert scheduled["later"] == []


@pytest.mark.asyncio
async def test_the_verdict_runs_after_the_longest_wait(monkeypatch, scheduled):
    _gpu(monkeypatch, True)
    order_id = uuid.uuid4()

    await work_orders.verify_order_semantics_later(order_id, work_orders._VERIFY_MAX_DEFERRALS)

    assert scheduled["verified"] == [order_id]
    assert scheduled["later"] == []


@pytest.mark.asyncio
async def test_only_a_verifier_on_strata_waits(monkeypatch):
    from app.ai import model_resolver
    from app.ai.model_resolver import ModelConfig

    monkeypatch.setattr(
        model_resolver,
        "get_reasoning_model",
        lambda confidential=True: ModelConfig(model="gemma4:26b", provider="ollama"),
    )

    async def _busy(**kwargs):
        raise AssertionError("Strata is not asked for an Ollama verifier")

    monkeypatch.setattr(strata_manager, "busy", _busy)
    assert await work_orders._verifier_should_wait() is False
