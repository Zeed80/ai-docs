"""Thinking-level probe: a verdict is cached, and it never blocks the page.

Live (2026-10-06): the Assignment tab took ~130-165 s to open. live-models
awaited a two-generation probe per thinking model per node, and the
thinkingcap template answered `think: "high"` with HTTP 500 "Unexpected
reasoning effort" — taken for an outage, never cached, re-run on every open.
"""

from __future__ import annotations

import asyncio

import pytest

from app.api import providers_api as pa


class _Resp:
    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _install(monkeypatch, responses: dict[str, _Resp]):
    class Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def post(self, _url, json):
            return responses[json["think"]]

    monkeypatch.setattr(pa.httpx, "AsyncClient", Client)


def test_template_refusing_a_level_is_a_cached_verdict(monkeypatch):
    _install(
        monkeypatch,
        {
            "low": _Resp(200, {"message": {"thinking": ""}}),
            "high": _Resp(
                500,
                text='{"error":"raise_exception(\'Unexpected reasoning effort \' ~ reasoning_effort)"}',
            ),
        },
    )
    assert asyncio.run(pa._ollama_probe_thinking_levels("http://o", "thinkingcap")) is False


def test_an_outage_stays_unknown(monkeypatch):
    _install(
        monkeypatch,
        {
            "low": _Resp(200, {"message": {"thinking": "a"}}),
            "high": _Resp(500, text='{"error":"model requires more system memory"}'),
        },
    )
    assert asyncio.run(pa._ollama_probe_thinking_levels("http://o", "m")) is None


def test_probe_is_scheduled_once_and_respects_cooldown_and_strata(monkeypatch):
    from app.ai import gpu_runtime

    started: list[str] = []

    async def fake_run(key, base_url, provider_model):
        started.append(key)
        pa._LEVEL_PROBE_TASKS.pop(key, None)

    monkeypatch.setattr(pa, "_run_level_probe", fake_run)
    monkeypatch.setattr(pa, "_level_probe_cooling_down", lambda key: key == "cooling")
    monkeypatch.setattr(gpu_runtime, "is_gpu_ollama", lambda url: url == "http://gpu")
    owner = {"v": "ollama"}
    monkeypatch.setattr(gpu_runtime, "current_owner", lambda: owner["v"])

    async def scenario():
        assert pa._schedule_level_probe("m", "http://gpu", "m") is True
        # Already running: no second probe for the same model.
        assert pa._schedule_level_probe("m", "http://gpu", "m") is False
        await asyncio.sleep(0)
        assert pa._schedule_level_probe("cooling", "http://gpu", "c") is False
        owner["v"] = "strata"
        assert pa._schedule_level_probe("x", "http://gpu", "x") is False
        assert pa._schedule_level_probe("y", "http://cpu", "y") is True
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert started == ["m", "y"]


def test_background_probe_persists_the_verdict(monkeypatch):
    from app.ai import model_registry, model_runtime_store

    overrides: dict = {}
    persisted: list = []

    async def probe(_url, _pm):
        return False

    async def persist(_db, **kw):
        persisted.append(kw)

    async def noop(*_a, **_k):
        return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def commit(self):
            return None

    monkeypatch.setattr(pa, "_ollama_probe_thinking_levels", probe)
    monkeypatch.setattr(
        model_registry, "set_thinking_override", lambda key, levels: overrides.update({key: levels})
    )
    monkeypatch.setattr(model_runtime_store, "persist_model_override", persist)
    monkeypatch.setattr(model_runtime_store, "hydrate_runtime_cache", noop)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: Session)

    asyncio.run(pa._run_level_probe("tc", "http://gpu", "thinkingcap"))
    assert overrides == {"tc": []}
    assert persisted == [{"model_key": "tc", "thinking_levels": []}]


@pytest.fixture(autouse=True)
def _clean_tasks():
    pa._LEVEL_PROBE_TASKS.clear()
    yield
    pa._LEVEL_PROBE_TASKS.clear()
