"""Tests for on-demand VRAM lifecycle: pinned orchestrator + ephemeral models."""

import pytest

from app.ai import model_lifecycle as ml
from app.ai.schemas import AITask


@pytest.fixture(autouse=True)
def _clear_cache():
    ml.invalidate_cache()
    yield
    ml.invalidate_cache()


def test_pinned_resolves_orchestrator(monkeypatch):
    def fake_resolve(task):
        return ("qwen3.5:9b", "ollama") if task == AITask.ORCHESTRATOR_PLANNING else (None, None)

    monkeypatch.setattr("app.ai.task_routing.resolve_model", fake_resolve)
    ml.invalidate_cache()
    assert ml.pinned_ollama_models() == {"qwen3.5:9b"}


def test_cloud_orchestrator_not_pinned(monkeypatch):
    # If the orchestrator is a cloud model, nothing is pinned for Ollama.
    monkeypatch.setattr(
        "app.ai.task_routing.resolve_model",
        lambda task: ("claude-sonnet-4-6", "anthropic"),
    )
    ml.invalidate_cache()
    assert ml.pinned_ollama_models() == set()


def test_keep_alive_pinned_vs_ephemeral(monkeypatch):
    monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)
    monkeypatch.setattr(ml, "pinned_ollama_models", lambda: {"qwen3.5:9b"})
    assert ml.keep_alive_for("qwen3.5:9b") == -1          # orchestrator pinned forever
    assert ml.keep_alive_for("gemma4:e4b") == ml.ephemeral_keep_alive()
    assert ml.keep_alive_for("nomic-embed-text") == "5m"  # embeddings kept a bit longer


def test_global_env_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "0")
    monkeypatch.setattr(ml, "pinned_ollama_models", lambda: {"qwen3.5:9b"})
    # Global override wins even for the pinned model.
    assert ml.keep_alive_for("qwen3.5:9b") == "0"


def test_ephemeral_configurable(monkeypatch):
    monkeypatch.setenv("OLLAMA_EPHEMERAL_KEEP_ALIVE", "120s")
    assert ml.ephemeral_keep_alive() == "120s"


# ── gpu_manager eviction preserves pinned ───────────────────────────────────────

class _FakeResp:
    status_code = 200

    def json(self):
        return {"models": [{"name": "qwen3.5:9b"}, {"name": "gemma4:e4b"}]}


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return _FakeResp()


@pytest.mark.asyncio
async def test_unload_all_skips_pinned(monkeypatch):
    from app.ai import gpu_manager

    monkeypatch.setattr(gpu_manager.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(ml, "pinned_ollama_models", lambda: {"qwen3.5:9b"})

    unloaded_calls: list[str] = []

    async def fake_unload(name, url=None):
        unloaded_calls.append(name)
        return True

    monkeypatch.setattr(gpu_manager, "unload_ollama_model", fake_unload)

    result = await gpu_manager.unload_all_ollama_models()
    assert "gemma4:e4b" in result
    assert "qwen3.5:9b" not in result        # pinned preserved
    assert unloaded_calls == ["gemma4:e4b"]


@pytest.mark.asyncio
async def test_unload_all_force_includes_pinned(monkeypatch):
    from app.ai import gpu_manager

    monkeypatch.setattr(gpu_manager.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(ml, "pinned_ollama_models", lambda: {"qwen3.5:9b"})

    async def fake_unload(name, url=None):
        return True

    monkeypatch.setattr(gpu_manager, "unload_ollama_model", fake_unload)

    result = await gpu_manager.unload_all_ollama_models(exclude_pinned=False)
    assert set(result) == {"qwen3.5:9b", "gemma4:e4b"}
