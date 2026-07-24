"""Auto-start of lazy single-model local servers (vLLM / llama.cpp) when their
model is assigned as a provider."""

import asyncio
import types

import pytest

from app.ai.schemas import ProviderKind
from app.api import providers_api


def _cap(provider: ProviderKind, provider_model: str = "some/model", vram: float = 5.0):
    return types.SimpleNamespace(
        provider=provider, provider_model=provider_model, vram_gb_estimate=vram,
        local_only=True,
    )


def _registry(models: dict):
    return types.SimpleNamespace(models=models)


@pytest.mark.asyncio
async def test_vllm_assignment_triggers_ensure_model_active(monkeypatch):
    calls = {}

    async def _fake_ensure_model_active(model_path):
        calls["vllm"] = model_path
        return {"status": "ok"}

    monkeypatch.setattr(
        "app.ai.providers.vllm_manager.ensure_model_active", _fake_ensure_model_active
    )
    # VRAM pre-check is advisory — stub it so the test needs no GPU.
    async def _noop_vram(*a, **k):
        return True, ""

    monkeypatch.setattr("app.ai.gpu_manager.ensure_vram_for", _noop_vram)

    reg = _registry({"m": _cap(ProviderKind.VLLM, "Qwen/Qwen3-VL-8B-Instruct")})
    await providers_api._autostart_assigned_provider("m", reg)

    assert calls["vllm"] == "Qwen/Qwen3-VL-8B-Instruct"


@pytest.mark.asyncio
async def test_generic_vllm_entry_only_ensures_server_running(monkeypatch):
    """A generic 'local' vLLM entry must not try to activate a model named
    'local' — it only needs the server up (vLLM serves one model as 'local')."""
    calls = {}

    async def _fake_ensure_running():
        calls["ran"] = True
        return {"status": "already_running"}

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("generic entry must not activate a model")

    monkeypatch.setattr("app.ai.providers.vllm_manager.ensure_server_running", _fake_ensure_running)
    monkeypatch.setattr("app.ai.providers.vllm_manager.ensure_model_active", _boom)

    async def _noop_vram(*a, **k):
        return True, ""

    monkeypatch.setattr("app.ai.gpu_manager.ensure_vram_for", _noop_vram)

    reg = _registry({"m": _cap(ProviderKind.VLLM, "local")})
    await providers_api._autostart_assigned_provider("m", reg)

    assert calls.get("ran") is True


@pytest.mark.asyncio
async def test_llamacpp_assignment_triggers_ensure_server_running(monkeypatch):
    calls = {}

    async def _fake_ensure_running():
        calls["llamacpp"] = True
        return {"status": "started"}

    monkeypatch.setattr(
        "app.ai.providers.llamacpp_manager.ensure_server_running", _fake_ensure_running
    )

    async def _noop_vram(*a, **k):
        return True, ""

    monkeypatch.setattr("app.ai.gpu_manager.ensure_vram_for", _noop_vram)

    reg = _registry({"m": _cap(ProviderKind.LLAMACPP, "local")})
    await providers_api._autostart_assigned_provider("m", reg)

    assert calls.get("llamacpp") is True


@pytest.mark.asyncio
async def test_ollama_and_cloud_models_are_noops(monkeypatch):
    """Always-on / remote providers must not attempt a container start."""
    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("no server start for ollama/cloud")

    monkeypatch.setattr("app.ai.providers.vllm_manager.ensure_model_active", _boom)
    monkeypatch.setattr("app.ai.providers.llamacpp_manager.ensure_server_running", _boom)

    reg = _registry({"o": _cap(ProviderKind.OLLAMA), "c": _cap(ProviderKind.ANTHROPIC)})
    await providers_api._autostart_assigned_provider("o", reg)
    await providers_api._autostart_assigned_provider("c", reg)


@pytest.mark.asyncio
async def test_scheduler_starts_one_model_per_lazy_kind(monkeypatch):
    """A single-model server can serve only one model, so a batch assigning two
    vLLM models must start exactly one (the last) — never race two restarts."""
    started: list[str] = []

    async def _record(model_key, registry):
        started.append(model_key)

    monkeypatch.setattr(providers_api, "_autostart_assigned_provider", _record)

    reg = _registry({
        "v1": _cap(ProviderKind.VLLM, "a"),
        "v2": _cap(ProviderKind.VLLM, "b"),
        "l1": _cap(ProviderKind.LLAMACPP, "local"),
        "o1": _cap(ProviderKind.OLLAMA),
    })
    providers_api._schedule_provider_autostart(["v1", "v2", "l1", "o1"], reg)
    await asyncio.sleep(0)  # let the fire-and-forget tasks run

    # one vLLM (the last, v2) + one llama.cpp, no ollama.
    assert set(started) == {"v2", "l1"}
