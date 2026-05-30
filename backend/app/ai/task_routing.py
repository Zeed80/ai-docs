"""Single source of truth for task → models routing.

Every AI call resolves "which models to try, with what inference profile and
local/cloud policy" through this store. It replaces the legacy split between
``ai_config`` (model_ocr / model_reasoning / ...) and the hard-coded
``model_registry.yaml`` ``fallback_chain``.

Per-task config (:class:`TaskRouting`):
  - ``models``  — ordered catalog keys; ``models[0]`` is primary, the rest are
                  fallbacks tried in order by :meth:`AIRouter.run`.
  - ``profile`` — inference parameter profile name (see ``parameter_profiles``).
  - ``local_only`` / ``allow_cloud`` — confidentiality policy.

Storage: Redis key ``task_routing`` (JSON), overlaid on defaults derived from
``model_registry.yaml`` routes + ``parameter_profiles.TASK_DEFAULT_PROFILE``.
The YAML remains the canonical defaults source; ``reset_task_routing`` returns
to it.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from pydantic import BaseModel, Field

from app.ai.parameter_profiles import TASK_DEFAULT_PROFILE, get_all_profiles
from app.ai.schemas import AITask

logger = structlog.get_logger()

_REDIS_KEY = "task_routing"
_REGISTRY_PATH = "backend/app/ai/config/model_registry.yaml"

# Tasks that process confidential documents → must stay strictly local.
# For these, local_only is forced True and cloud is never allowed (UI locks it).
CONFIDENTIAL_TASKS: set[AITask] = {
    AITask.INVOICE_OCR,
    AITask.STRUCTURED_EXTRACTION,
    AITask.DRAWING_ANALYSIS,
    AITask.DRAWING_ANALYSIS_VLM,
    AITask.EMBEDDING,
    AITask.RERANKING,
}


class TaskRouting(BaseModel):
    """Resolved routing for a single task."""

    task: str
    models: list[str] = Field(default_factory=list)  # primary = models[0]
    profile: str = "balanced"
    local_only: bool = True
    allow_cloud: bool = False

    @property
    def primary(self) -> str | None:
        return self.models[0] if self.models else None


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

def _redis_get() -> dict[str, dict] | None:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_REDIS_KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _redis_set(value: dict[str, dict]) -> None:
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().set(_REDIS_KEY, json.dumps(value, ensure_ascii=False))
    except Exception as exc:
        logger.warning("task_routing_redis_write_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Catalog helpers (defaults + validation)
# ---------------------------------------------------------------------------

_defaults_cache: dict[str, TaskRouting] | None = None


def _registry() -> Any:
    from app.ai.model_registry import ModelRegistry

    return ModelRegistry.from_yaml(_REGISTRY_PATH)


def _registry_defaults() -> dict[str, TaskRouting]:
    """Default routing per task from the YAML registry (routes never change at runtime)."""
    global _defaults_cache
    if _defaults_cache is not None:
        return _defaults_cache
    out: dict[str, TaskRouting] = {}
    try:
        reg = _registry()
        routes = reg.routes
    except Exception as exc:
        logger.warning("task_routing_registry_unavailable", error=str(exc))
        routes = {}
    for task in AITask:
        route = routes.get(task)
        models = list(route.fallback_chain) if route else []
        local_only = route.local_only if route else True
        out[task.value] = _enforce_confidential(
            task,
            TaskRouting(
                task=task.value,
                models=models,
                profile=TASK_DEFAULT_PROFILE.get(task, "balanced"),
                local_only=local_only,
                allow_cloud=not local_only,
            ),
        )
    _defaults_cache = out
    return out


def known_model_keys() -> set[str]:
    """All catalog model keys, including runtime-added (overlay) entries."""
    try:
        return set(_registry().models.keys())
    except Exception:
        return set()


def _enforce_confidential(task: AITask, routing: TaskRouting) -> TaskRouting:
    if task in CONFIDENTIAL_TASKS and (not routing.local_only or routing.allow_cloud):
        return routing.model_copy(update={"local_only": True, "allow_cloud": False})
    return routing


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_task_routing() -> dict[AITask, TaskRouting]:
    """Return effective routing for every task (defaults overlaid with Redis)."""
    defaults = _registry_defaults()
    overlay = _redis_get() or {}
    result: dict[AITask, TaskRouting] = {}
    for task in AITask:
        base = defaults.get(task.value) or TaskRouting(task=task.value)
        ov = overlay.get(task.value)
        if ov:
            merged = {**base.model_dump(), **ov, "task": task.value}
            try:
                routing = TaskRouting(**merged)
            except Exception:
                routing = base
        else:
            routing = base
        result[task] = _enforce_confidential(task, routing)
    return result


def get_routing_for(task: AITask) -> TaskRouting:
    return get_task_routing()[task]


def resolve_model(task: AITask) -> tuple[str | None, str | None]:
    """Resolve a task's primary catalog key to ``(provider_model, provider_str)``.

    Used by direct-call sites (drawing_extractor, telegram, extraction helpers,
    reasoning_generate) that don't go through :meth:`AIRouter.run` but must read
    the same routing store. Returns ``(None, None)`` if unresolved.
    """
    key = get_routing_for(task).primary
    if not key:
        return None, None
    try:
        cap = _registry().models.get(key)
    except Exception:
        cap = None
    if cap is not None:
        return cap.provider_model, cap.provider.value
    return None, None


def _validate(routing: TaskRouting) -> None:
    keys = known_model_keys()
    unknown = [m for m in routing.models if m not in keys]
    if unknown:
        raise ValueError(f"Unknown model keys: {', '.join(unknown)}")
    if routing.profile not in get_all_profiles():
        raise ValueError(f"Unknown inference profile: {routing.profile}")


def save_task_routing(task: AITask, routing: TaskRouting) -> TaskRouting:
    """Persist a per-task routing override (validated, confidentiality enforced)."""
    routing = routing.model_copy(update={"task": task.value})
    routing = _enforce_confidential(task, routing)
    _validate(routing)
    overlay = _redis_get() or {}
    overlay[task.value] = routing.model_dump()
    _redis_set(overlay)
    logger.info("task_routing_saved", task=task.value, models=routing.models, profile=routing.profile)
    return routing


def reset_task_routing(task: AITask) -> TaskRouting:
    """Drop the override for a task, reverting to the YAML default."""
    overlay = _redis_get() or {}
    overlay.pop(task.value, None)
    _redis_set(overlay)
    return get_routing_for(task)


# ---------------------------------------------------------------------------
# One-time migration from legacy ai_config (model_ocr / model_vlm / ...)
# ---------------------------------------------------------------------------

# Legacy ai_config field → tasks it used to drive.
_LEGACY_FIELD_TASKS: dict[str, list[AITask]] = {
    "model_ocr": [AITask.INVOICE_OCR, AITask.CLASSIFICATION, AITask.STRUCTURED_EXTRACTION],
    "model_vlm": [AITask.DRAWING_ANALYSIS, AITask.DRAWING_ANALYSIS_VLM],
    "model_reasoning": [AITask.ENGINEERING_REASONING, AITask.EMAIL_DRAFTING],
}


def _catalog_key_for(model_name: str, provider: str) -> str | None:
    """Find a catalog key whose provider_model + provider matches a legacy raw name."""
    try:
        reg = _registry()
    except Exception:
        return None
    for key, cap in reg.models.items():
        if cap.provider_model == model_name and cap.provider.value == provider:
            return key
    return None


def migrate_from_ai_config() -> dict[str, Any]:
    """If no task_routing override exists yet, seed it from the legacy ai_config.

    Best-effort: legacy values are stored as raw model names + provider strings.
    We resolve each to a catalog key; unresolved entries are skipped (the task
    keeps its YAML default) and logged. Idempotent — does nothing once an
    override exists.
    """
    if _redis_get():
        return {"migrated": False, "reason": "task_routing already present"}

    try:
        from app.api.ai_settings import get_ai_config

        cfg = get_ai_config()
    except Exception as exc:
        return {"migrated": False, "reason": f"ai_config unavailable: {exc}"}

    overlay: dict[str, dict] = {}
    resolved: list[str] = []
    skipped: list[str] = []
    defaults = _registry_defaults()

    for field, tasks in _LEGACY_FIELD_TASKS.items():
        model_name = (cfg.get(field) or "").strip()
        provider = (cfg.get(f"{field}_provider") or "ollama").strip()
        if not model_name:
            continue
        key = _catalog_key_for(model_name, provider)
        if not key:
            skipped.append(f"{field}={model_name}({provider})")
            continue
        is_cloud = provider not in ("ollama", "llamacpp", "vllm", "lmstudio", "openai_compatible")
        for task in tasks:
            base = defaults.get(task.value) or TaskRouting(task=task.value)
            # Put the legacy model first; keep YAML fallbacks after it.
            chain = [key] + [m for m in base.models if m != key]
            update: dict[str, Any] = {"models": chain}
            # A cloud legacy model is only honoured for non-confidential tasks;
            # _enforce_confidential re-locks confidential ones to local.
            if is_cloud:
                update["local_only"] = False
                update["allow_cloud"] = True
            routing = _enforce_confidential(task, base.model_copy(update=update))
            overlay[task.value] = routing.model_dump()
            resolved.append(f"{task.value}→{key}")

    if overlay:
        _redis_set(overlay)
    logger.info("task_routing_migrated", resolved=resolved, skipped=skipped)
    return {"migrated": bool(overlay), "resolved": resolved, "skipped": skipped}
