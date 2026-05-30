"""Live, full-coverage tests for local-model functions across all providers.

Exercises the real machinery behind Settings → Модели on whichever local
providers are actually running (Ollama / llama.cpp / vLLM). Each provider's
tests skip when its server is unreachable, so the file is safe to run anywhere
and gives real coverage on the stack.

Run inside the backend container (Redis/Postgres/Ollama reachable):
  docker exec infra-backend-1 pytest tests/test_local_models_live.py -m live -s

Marks: @pytest.mark.live (needs at least one local provider + Redis).
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from app.ai.schemas import AITask

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Provider availability probes
# ---------------------------------------------------------------------------

def _http_ok(url: str, path: str) -> bool:
    try:
        return httpx.get(f"{url.rstrip('/')}{path}", timeout=3.0).status_code < 500
    except Exception:
        return False


def _ollama_up() -> bool:
    return _http_ok(os.environ.get("OLLAMA_URL", "http://host-gateway:11434"), "/api/tags")


def _llamacpp_up() -> bool:
    return _http_ok(os.environ.get("LLAMACPP_URL", "http://llama-server:8080"), "/health")


def _vllm_up() -> bool:
    return _http_ok(os.environ.get("VLLM_URL", "http://vllm-server:8000"), "/v1/models")


def _redis_up() -> bool:
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().ping()
        return True
    except Exception:
        return False


PROVIDERS = {"ollama": _ollama_up, "llamacpp": _llamacpp_up, "vllm": _vllm_up}
AVAILABLE = [name for name, up in PROVIDERS.items() if up()]


# ---------------------------------------------------------------------------
# Status / GPU / allocations (provider-agnostic)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_endpoint_reports_providers():
    from app.api.local_models_api import get_all_providers_status

    status = await get_all_providers_status()
    assert "providers" in status
    for p in ("ollama", "llamacpp", "vllm"):
        assert p in status["providers"]
    # at least one provider should be reported running if any is up
    if AVAILABLE:
        assert any(status["providers"][p].get("running") for p in AVAILABLE)


@pytest.mark.asyncio
async def test_gpu_stats_and_allocations():
    from app.ai import gpu_manager

    gpu = await gpu_manager.get_gpu_stats()
    allocations = await gpu_manager.get_allocations()
    assert isinstance(allocations, dict)
    if gpu:
        assert gpu.total_gb > 0
        assert gpu.free_gb <= gpu.total_gb


@pytest.mark.parametrize("provider", AVAILABLE or ["__none__"])
@pytest.mark.asyncio
async def test_list_local_models(provider):
    if provider == "__none__":
        pytest.skip("no local provider available")
    from app.api.local_models_api import list_provider_models

    result = await list_provider_models(provider)
    assert result["provider"] == provider
    assert isinstance(result["models"], list)


# ---------------------------------------------------------------------------
# Routing CRUD (needs Redis) — full source-of-truth round trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_routing_crud_roundtrip():
    if not _redis_up():
        pytest.skip("Redis unavailable")
    from app.ai import task_routing as tr

    original = tr.get_routing_for(AITask.ENGINEERING_REASONING)
    try:
        catalog = list(tr.known_model_keys())
        local = [k for k in catalog if k.endswith("_ollama")]
        assert local, "expected local ollama models in catalog"
        new = original.model_copy(update={"models": local[:2], "profile": "balanced"})
        saved = tr.save_task_routing(AITask.ENGINEERING_REASONING, new)
        assert saved.models == local[:2]
        assert tr.get_routing_for(AITask.ENGINEERING_REASONING).profile == "balanced"
    finally:
        tr.reset_task_routing(AITask.ENGINEERING_REASONING)
    assert tr.get_routing_for(AITask.ENGINEERING_REASONING).models == original.models


@pytest.mark.asyncio
async def test_confidential_routing_cannot_be_set_cloud():
    if not _redis_up():
        pytest.skip("Redis unavailable")
    from app.ai import task_routing as tr

    cloud = next((k for k in tr.known_model_keys() if "anthropic" in k), None)
    if not cloud:
        pytest.skip("no cloud model in catalog")
    routing = tr.TaskRouting(
        task="invoice_ocr", models=[cloud], profile="anti_hallucination",
        local_only=False, allow_cloud=True,
    )
    with pytest.raises(ValueError):
        tr.save_task_routing(AITask.INVOICE_OCR, routing)
    # Default routing remains local-only.
    assert tr.get_routing_for(AITask.INVOICE_OCR).local_only is True


# ---------------------------------------------------------------------------
# Presets + telemetry (need Redis)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_preset_and_reset():
    if not _redis_up():
        pytest.skip("Redis unavailable")
    from app.ai import presets, task_routing as tr

    before = {t.value: tr.get_routing_for(t).model_dump() for t in AITask}
    try:
        result = presets.apply_preset("rtx3090_balanced")
        assert result["applied"]
        # engineering_reasoning primary should now match the preset
        assert tr.get_routing_for(AITask.ENGINEERING_REASONING).models[0] == "qwen3_5_9b_ollama"
    finally:
        for t in AITask:
            tr.reset_task_routing(t)
    # confidential tasks stay local regardless
    assert tr.get_routing_for(AITask.INVOICE_OCR).local_only is True


@pytest.mark.asyncio
async def test_telemetry_records_real_calls():
    if not _redis_up():
        pytest.skip("Redis unavailable")
    from app.ai import telemetry

    telemetry.record_call(
        task="classification", model="gemma4_e4b_ollama", provider="ollama",
        latency_ms=42, ok=True, input_tokens=7, output_tokens=3,
    )
    summary = telemetry.get_summary()
    assert summary["totals"]["calls"] >= 1
    assert any(r["model"] == "gemma4_e4b_ollama" for r in summary["by_model"])


# ---------------------------------------------------------------------------
# Real generation through AIRouter on each available local provider
# ---------------------------------------------------------------------------

def _installed_ollama_models() -> list[str]:
    url = os.environ.get("OLLAMA_URL", "http://host-gateway:11434")
    try:
        data = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=5.0).json()
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return []


@pytest.mark.slow
@pytest.mark.asyncio
async def test_ollama_real_generation_through_router():
    if "ollama" not in AVAILABLE:
        pytest.skip("Ollama not available")
    from app.ai.router import ai_router
    from app.ai.schemas import AIRequest, ChatMessage

    installed = _installed_ollama_models()
    if not installed:
        pytest.skip("no Ollama models installed")

    resp = await ai_router.run(
        AIRequest(
            task=AITask.CLASSIFICATION,
            messages=[ChatMessage(role="user", content="Ответь одним словом: счёт или письмо? Текст: 'Счёт на оплату №5'.")],
            confidential=True,
        )
    )
    assert resp.provider.value == "ollama"
    assert resp.text
    assert resp.usage.total_tokens is None or resp.usage.total_tokens >= 0


@pytest.mark.slow
@pytest.mark.asyncio
async def test_benchmark_endpoint_ollama():
    if "ollama" not in AVAILABLE:
        pytest.skip("Ollama not available")
    from app.api.local_models_api import BenchmarkRequest, benchmark_model
    from app.ai import task_routing as tr

    primary = tr.get_routing_for(AITask.CLASSIFICATION).primary
    if not primary or not primary.endswith("_ollama"):
        pytest.skip("classification not routed to ollama")
    result = await benchmark_model(BenchmarkRequest(model_key=primary, task="classification"))
    assert result["ok"] is True
    assert result["latency_ms"] > 0


# ---------------------------------------------------------------------------
# Provider matrix marker — documents what ran
# ---------------------------------------------------------------------------

def test_report_available_providers():
    print(f"\n  Available local providers: {AVAILABLE or 'none'}")
    # Always passes; informational.
    assert True
