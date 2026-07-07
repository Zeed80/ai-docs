"""AI provider health check endpoint.

GET /health/ai — checks connectivity for every registered AI provider.
Results are best-effort: a failed provider reports an error without aborting
the overall response. Cloud providers without an API key are skipped.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import structlog
from fastapi import APIRouter

from app.ai import provider_registry
from app.ai.router import ai_router
from app.ai.schemas import ProviderKind

logger = structlog.get_logger()

router = APIRouter()

_TIMEOUT = 8.0


async def _check_ollama(base_url: str) -> dict[str, Any]:
    """Hit /api/tags to verify Ollama is reachable."""
    try:
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code == 200:
            models = [m.get("name") for m in (resp.json().get("models") or [])]
            return {"ok": True, "latency_ms": elapsed_ms, "models": models[:10]}
        return {"ok": False, "status": resp.status_code, "latency_ms": elapsed_ms}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _check_openai_compatible(
    base_url: str, api_key: str | None, *, require_key: bool = True
) -> dict[str, Any]:
    """Hit /models to verify an OpenAI-compatible endpoint."""
    if require_key and not api_key:
        return {"ok": False, "skipped": True, "reason": "no_api_key"}
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {"ok": resp.status_code < 400, "status": resp.status_code, "latency_ms": elapsed_ms}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _check_anthropic(api_key: str | None) -> dict[str, Any]:
    """Hit Anthropic models list to verify the key is valid."""
    if not api_key:
        return {"ok": False, "skipped": True, "reason": "no_api_key"}
    try:
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {"ok": resp.status_code < 400, "status": resp.status_code, "latency_ms": elapsed_ms}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _check_comfyui(base_url: str) -> dict[str, Any]:
    """Hit ComfyUI's native health-ish endpoint instead of OpenAI /models."""
    try:
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/system_stats")
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {"ok": resp.status_code < 400, "status": resp.status_code, "latency_ms": elapsed_ms}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _effective_provider_base_url(kind: ProviderKind, fallback: str) -> str:
    if kind in {
        ProviderKind.OLLAMA,
        ProviderKind.LLAMACPP,
        ProviderKind.VLLM,
        ProviderKind.OPENAI_COMPATIBLE,
        ProviderKind.LMSTUDIO,
        ProviderKind.COMFYUI,
    }:
        try:
            nodes = provider_registry.list_instances(kind)
            if nodes:
                return nodes[0].base_url
        except Exception as exc:  # noqa: BLE001
            logger.debug("ai_provider_endpoint_resolve_failed", provider=kind.value, error=str(exc))
    return fallback


@router.get("/health/ai")
async def ai_health() -> dict[str, Any]:
    """Check connectivity for all registered AI providers."""
    results: dict[str, Any] = {}

    for kind, config in ai_router.registry.providers.items():
        base_url = _effective_provider_base_url(kind, str(config.base_url or "")).rstrip("/")
        api_key_env = config.api_key_env or ""
        api_key = os.environ.get(api_key_env) if api_key_env else None

        try:
            if kind == ProviderKind.OLLAMA:
                result = await _check_ollama(base_url)
            elif kind == ProviderKind.COMFYUI:
                result = await _check_comfyui(base_url)
            elif kind == ProviderKind.ANTHROPIC:
                result = await _check_anthropic(api_key)
            else:
                # Local providers (no api_key_env) don't require a key — just probe the URL
                require_key = bool(api_key_env)
                result = await _check_openai_compatible(base_url, api_key, require_key=require_key)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}

        results[kind.value] = result
        if not result.get("ok") and not result.get("skipped"):
            logger.warning("ai_provider_health_failed", provider=kind.value, **result)

    overall_ok = all(
        r.get("ok") or r.get("skipped")
        for r in results.values()
    )
    return {"ok": overall_ok, "providers": results}
