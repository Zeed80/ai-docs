"""Local model VRAM lifecycle: load on demand, pin the orchestrator.

Goal: keep VRAM free by loading task models only when needed, while the
orchestrator (agent brain) model stays resident for instant response.

Mechanism (Ollama, the dynamic multi-model provider):
  - The orchestrator model (ORCHESTRATOR_PLANNING route primary) is *pinned*:
    requests use ``keep_alive = -1`` (never auto-unload), and it is warmed at
    startup.
  - Every other model is *ephemeral*: a short ``keep_alive`` so it frees VRAM
    soon after it goes idle.
  - VRAM eviction (gpu_manager) never unloads pinned models.

llama.cpp / vLLM hold a single resident model by design (container-bound), so
their lifecycle is managed by activating/restarting, not by keep_alive.
"""

from __future__ import annotations

import os
import time

import structlog

from app.ai.schemas import AITask

logger = structlog.get_logger()

# Tasks whose primary model must always stay loaded (instant agent response).
PINNED_TASKS: set[AITask] = {AITask.ORCHESTRATOR_PLANNING}

_LOCAL = ("ollama",)  # only Ollama supports dynamic keep_alive load/unload

# Cache the resolved pinned set briefly so we don't hit Redis/registry per call.
_CACHE_TTL = 30.0
_pinned_cache: tuple[float, set[str]] | None = None


def ephemeral_keep_alive() -> str:
    """keep_alive for non-pinned models. Short so idle models free VRAM."""
    return os.environ.get("OLLAMA_EPHEMERAL_KEEP_ALIVE", "60s").strip() or "60s"


def pinned_ollama_models() -> set[str]:
    """Provider-model names that must stay resident (orchestrator), Ollama only."""
    global _pinned_cache
    now = time.monotonic()
    if _pinned_cache and now - _pinned_cache[0] < _CACHE_TTL:
        return _pinned_cache[1]

    pinned: set[str] = set()
    try:
        from app.ai.task_routing import resolve_model

        for task in PINNED_TASKS:
            model, provider = resolve_model(task)
            if model and provider in _LOCAL:
                pinned.add(model)
    except Exception as exc:
        logger.debug("pinned_models_resolve_failed", error=str(exc))

    _pinned_cache = (now, pinned)
    return pinned


def invalidate_cache() -> None:
    global _pinned_cache
    _pinned_cache = None


def keep_alive_for(model: str) -> str | int:
    """Return the Ollama keep_alive value for a model.

    -1 (forever) for the pinned orchestrator model; otherwise a short ephemeral
    duration. A global ``OLLAMA_KEEP_ALIVE`` env still overrides everything.
    """
    env_override = os.environ.get("OLLAMA_KEEP_ALIVE", "").strip()
    if env_override:
        return env_override
    if model in pinned_ollama_models():
        return -1
    # Embedding/reranker models are cheap and called in bursts → keep a bit longer.
    model_lower = model.lower()
    if any(x in model_lower for x in ("embed", "rerank", "nomic", "bge")):
        return "5m"
    return ephemeral_keep_alive()


async def warm_pinned(ollama_url: str | None = None) -> list[str]:
    """Preload pinned models into VRAM with keep_alive=-1 (called at startup)."""
    import httpx

    from app.config import settings

    url = (ollama_url or str(settings.ollama_url)).rstrip("/")
    warmed: list[str] = []
    for model in pinned_ollama_models():
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                # Empty prompt + keep_alive=-1 loads the model and keeps it resident.
                r = await client.post(
                    f"{url}/api/generate",
                    json={"model": model, "keep_alive": -1, "stream": False},
                )
                if r.status_code == 200:
                    warmed.append(model)
                    logger.info("pinned_model_warmed", model=model)
                else:
                    logger.warning("pinned_model_warm_failed", model=model, status=r.status_code)
        except Exception as exc:
            logger.warning("pinned_model_warm_error", model=model, error=str(exc))
    return warmed
