"""Inference parameter profiles for all AI tasks.

Profiles control temperature, top_p, repeat_penalty etc. per task type.
The goal is zero-hallucination for structured extraction tasks and
natural-sounding output for generative tasks.

Storage: Redis key ``inference_profiles`` (JSON) → fallback to DEFAULTS.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.ai.schemas import AITask

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

_BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "anti_hallucination": {
        "description": "Без галлюцинаций — для OCR, извлечения счётов и чертежей",
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": 1,
        "repeat_penalty": 1.1,
        "min_p": 0.0,
    },
    "structured_reasoning": {
        "description": "Структурированное рассуждение — для tool_calling, планирования агента",
        "temperature": 0.15,
        "top_p": 0.95,
        "top_k": 40,
        "repeat_penalty": 1.05,
        "min_p": 0.0,
    },
    "balanced": {
        "description": "Баланс качества и разнообразия — для общего использования",
        "temperature": 0.3,
        "top_p": 0.95,
        "top_k": 40,
        "repeat_penalty": 1.05,
        "min_p": 0.0,
    },
    "creative": {
        "description": "Творческий режим — для генерации писем и текстовых отчётов",
        "temperature": 0.7,
        "top_p": 0.95,
        "top_k": 50,
        "repeat_penalty": 1.0,
        "min_p": 0.0,
    },
}

# Default profile name per task
TASK_DEFAULT_PROFILE: dict[AITask, str] = {
    AITask.INVOICE_OCR: "anti_hallucination",
    AITask.STRUCTURED_EXTRACTION: "anti_hallucination",
    AITask.DRAWING_ANALYSIS: "anti_hallucination",
    AITask.DRAWING_ANALYSIS_VLM: "anti_hallucination",
    AITask.CLASSIFICATION: "anti_hallucination",
    AITask.ENGINEERING_REASONING: "structured_reasoning",
    AITask.TOOL_CALLING: "structured_reasoning",
    AITask.ORCHESTRATOR_PLANNING: "structured_reasoning",
    AITask.CODE_GENERATION: "structured_reasoning",
    AITask.EMAIL_DRAFTING: "creative",
    AITask.LONG_CONTEXT_SUMMARIZATION: "balanced",
    AITask.EMBEDDING: "balanced",
    AITask.RERANKING: "balanced",
    AITask.SPEECH: "balanced",
}

# Hardware-specific provider defaults for RTX 3090 24 GB
PROVIDER_HARDWARE_DEFAULTS: dict[str, dict[str, Any]] = {
    "llamacpp": {
        "n_gpu_layers": -1,        # all layers on GPU
        "kv_cache_type": "q8_0",   # 2× memory savings, minimal quality loss
        "flash_attn": True,
        "ctx_size": 16384,
        "parallel": 2,
    },
    "ollama": {
        "num_gpu": 1,
        "num_ctx": 8192,
        "max_loaded_models": 2,
    },
    "vllm": {
        "gpu_memory_utilization": 0.85,
        "dtype": "bfloat16",
        "max_model_len": 16384,
        "tensor_parallel_size": 1,
    },
}

_REDIS_KEY = "inference_profiles"
_TASK_OVERRIDES_KEY = "inference_task_profile_overrides"


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

def _redis_get(key: str) -> dict | None:
    try:
        from app.utils.redis_client import get_sync_redis
        raw = get_sync_redis().get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _redis_set(key: str, value: dict) -> None:
    try:
        from app.utils.redis_client import get_sync_redis
        get_sync_redis().set(key, json.dumps(value, ensure_ascii=False))
    except Exception as exc:
        logger.warning("inference_profiles_redis_write_failed", key=key, error=str(exc))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_all_profiles() -> dict[str, dict[str, Any]]:
    """Return all profiles (built-in + user-saved), built-in always present."""
    custom = _redis_get(_REDIS_KEY) or {}
    return {**_BUILTIN_PROFILES, **custom}


def get_profile(name: str) -> dict[str, Any] | None:
    """Return a single profile by name or None if not found."""
    return get_all_profiles().get(name)


def save_custom_profile(name: str, params: dict[str, Any]) -> None:
    """Persist a custom (user-defined) profile. Built-in names are protected."""
    if name in _BUILTIN_PROFILES:
        raise ValueError(f"Profile '{name}' is built-in and cannot be overwritten.")
    custom = _redis_get(_REDIS_KEY) or {}
    custom[name] = params
    _redis_set(_REDIS_KEY, custom)


def delete_custom_profile(name: str) -> None:
    if name in _BUILTIN_PROFILES:
        raise ValueError(f"Profile '{name}' is built-in and cannot be deleted.")
    custom = _redis_get(_REDIS_KEY) or {}
    custom.pop(name, None)
    _redis_set(_REDIS_KEY, custom)


def get_task_profile_overrides() -> dict[str, str]:
    """Return task→profile overrides set by user (task_name → profile_name)."""
    return _redis_get(_TASK_OVERRIDES_KEY) or {}


def set_task_profile_override(task: AITask, profile_name: str) -> None:
    overrides = get_task_profile_overrides()
    overrides[task.value] = profile_name
    _redis_set(_TASK_OVERRIDES_KEY, overrides)


def get_inference_params(task: AITask) -> dict[str, Any]:
    """Return final inference params for a task (override → default → built-in)."""
    overrides = get_task_profile_overrides()
    profile_name = overrides.get(task.value) or TASK_DEFAULT_PROFILE.get(task, "balanced")
    profile = get_profile(profile_name)
    if profile is None:
        profile = _BUILTIN_PROFILES["balanced"]
    # Strip metadata fields
    return {k: v for k, v in profile.items() if k != "description"}
