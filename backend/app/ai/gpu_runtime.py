"""Who owns the GPU right now: GPU-Ollama or Strata.

Strata fills the whole card with its expert cache and keeps 30-50 GB of
experts in RAM; a GPU-Ollama model loaded beside it does not fail loudly —
Ollama quietly spills it to CPU and the RAM it takes is the RAM Strata's
experts live in. So the two are never run side by side: the operator switches
the GPU between them (``/api/local-models/strata/switch``) and this module is
the single answer to "may this call go out now?".

Scope of the guard: the GPU Ollama node is the DEFAULT Ollama node (the
host server). Extra Ollama nodes — the CPU node for embeddings/reranking,
other machines — are unaffected by the switch.

An unreadable owner is *unknown*, not "ollama": a Redis blip must not start
refusing calls to a Strata that is actually serving.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

OWNER_KEY = "gpu_runtime:owner"
OLLAMA = "ollama"
STRATA = "strata"
OWNERS = (OLLAMA, STRATA)


class GpuRuntimeUnavailable(RuntimeError):
    """The call targets the runtime that does not own the GPU right now."""


def current_owner() -> str | None:
    """``"ollama"`` / ``"strata"``; ``"ollama"`` when never switched; None if unreadable."""
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(OWNER_KEY)
    except Exception as exc:  # noqa: BLE001 — unknown, not a decision
        logger.warning("gpu_runtime_owner_unreadable", error=str(exc)[:200])
        return None
    if raw is None:
        return OLLAMA
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    return value if value in OWNERS else OLLAMA


def set_owner(owner: str) -> None:
    if owner not in OWNERS:
        raise ValueError(f"unknown GPU runtime: {owner}")
    from app.utils.redis_client import get_sync_redis

    get_sync_redis().set(OWNER_KEY, owner)
    logger.info("gpu_runtime_owner_set", owner=owner)


def _norm(url: str | None) -> str:
    return (url or "").strip().rstrip("/").removesuffix("/v1").lower()


def is_gpu_ollama(base_url: str | None) -> bool:
    """Is ``base_url`` the default (GPU) Ollama node?"""
    if not base_url:
        return True
    try:
        from app.ai.provider_registry import _default_instance
        from app.ai.schemas import ProviderKind

        default = _default_instance(ProviderKind.OLLAMA).base_url
    except Exception:  # noqa: BLE001
        from app.config import settings

        default = settings.ollama_url
    return _norm(base_url) == _norm(default)


def check_call(provider: str, base_url: str | None = None) -> None:
    """Raise ``GpuRuntimeUnavailable`` if ``provider`` may not be called now."""
    if provider not in (OLLAMA, STRATA):
        return
    owner = current_owner()
    if owner is None:
        return
    if provider == STRATA and owner != STRATA:
        raise GpuRuntimeUnavailable(
            "Strata выключена: видеокарта отдана Ollama. Включите Strata в «Модели → "
            "Провайдеры» или назначьте слот на модель Ollama."
        )
    if provider == OLLAMA and owner == STRATA and is_gpu_ollama(base_url):
        raise GpuRuntimeUnavailable(
            "Видеокарта отдана Strata: модели Ollama на GPU сейчас не запускаются. "
            "Назначьте слот на Strata или переключите видеокарту обратно на Ollama "
            "в «Модели → Провайдеры»."
        )
