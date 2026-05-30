"""Model+provider name resolution for direct-call sites.

The single source of truth for "which model for which task" is
``app.ai.task_routing`` (editable from the Settings → Модели → Маршрутизация
UI). This module is a thin adapter that turns a task's primary catalog key into
a concrete ``(model_name, provider)`` pair for code paths that call providers
directly instead of through :meth:`AIRouter.run` (drawing_extractor, telegram,
extraction helpers, ``reasoning_generate``).

It no longer reads the legacy ``ai_config`` store. Environment/pydantic defaults
are used only as a last-resort fallback when routing yields nothing.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass

from app.ai.schemas import AITask
from app.config import settings

logger = structlog.get_logger()

_LOCAL_PROVIDERS = ("ollama", "llamacpp", "vllm", "lmstudio", "openai_compatible")


@dataclass
class ModelConfig:
    """Resolved model + provider pair for a task."""

    model: str
    # Provider string matches the keys used in the UI and ai_config:
    # "ollama" | "llamacpp" | "vllm" | "lmstudio" | "openai_compatible"
    # | "openrouter" | "openai" | "anthropic" | "deepseek" | "gemini"
    # | "mistral" | "groq" | "together" | "fireworks" | "xai"
    # | "cohere" | "perplexity" | "minimax" | "kimi" | "qwen"
    provider: str

    @property
    def is_local(self) -> bool:
        return self.provider in ("ollama", "llamacpp", "vllm", "lmstudio", "openai_compatible")

    @property
    def is_cloud(self) -> bool:
        return not self.is_local


def _resolve(task: AITask, fallback_model: str) -> tuple[str, str]:
    """Resolve a task's primary model via task_routing, with an env fallback."""
    try:
        from app.ai.task_routing import resolve_model
        model, provider = resolve_model(task)
    except Exception as exc:
        logger.warning("model_resolver_routing_unavailable", task=task.value, error=str(exc))
        model, provider = None, None
    return (model or fallback_model), (provider or "ollama")


def get_ocr_model() -> ModelConfig:
    """Model for OCR / invoice extraction. Must be local (documents are confidential)."""
    model, provider = _resolve(AITask.INVOICE_OCR, settings.ollama_model_ocr)
    if provider not in _LOCAL_PROVIDERS:
        logger.warning("model_resolver_ocr_cloud_blocked", provider=provider, model=model)
        provider = "ollama"
    return ModelConfig(model=model, provider=provider)


def get_vlm_model() -> ModelConfig:
    """Vision Language Model for drawing / image analysis. Must be local."""
    model, provider = _resolve(AITask.DRAWING_ANALYSIS_VLM, settings.ollama_model_vlm)
    if provider not in _LOCAL_PROVIDERS:
        logger.warning("model_resolver_vlm_cloud_blocked", provider=provider, model=model)
        provider = "ollama"
    return ModelConfig(model=model, provider=provider)


def get_reasoning_model(confidential: bool = False) -> ModelConfig:
    """Model for reasoning tasks. Cloud providers allowed when confidential=False."""
    model, provider = _resolve(AITask.ENGINEERING_REASONING, settings.ollama_model_reasoning)
    # Enforce local-only for confidential tasks
    if confidential and provider not in _LOCAL_PROVIDERS:
        logger.warning(
            "model_resolver_reasoning_cloud_blocked_confidential",
            provider=provider, model=model,
        )
        provider = "ollama"
        model = settings.ollama_model_reasoning
    return ModelConfig(model=model, provider=provider)


def get_verify_model() -> ModelConfig:
    """Model for extraction verification. Must be local."""
    model, provider = _resolve(AITask.STRUCTURED_EXTRACTION, settings.ollama_model_ocr)
    if provider not in _LOCAL_PROVIDERS:
        provider = "ollama"
    return ModelConfig(model=model, provider=provider)


# ---------------------------------------------------------------------------
# Provider URL helpers (used by dispatch functions)
# ---------------------------------------------------------------------------

def _provider_base_url(provider: str) -> str:
    """Return the base URL (without /v1) for OpenAI-compatible providers."""
    import os

    if provider == "llamacpp":
        return str(settings.llamacpp_url).rstrip("/").rstrip("/v1")
    if provider == "vllm":
        url = os.environ.get("VLLM_URL", "").strip()
        return url.rstrip("/v1") if url else "http://localhost:8001"
    if provider == "lmstudio":
        url = os.environ.get("LMSTUDIO_URL", "").strip()
        return url.rstrip("/v1") if url else "http://localhost:1234"
    if provider == "openai_compatible":
        url = os.environ.get("OPENAI_COMPATIBLE_URL", "").strip()
        return url.rstrip("/v1") if url else "http://localhost:8001"
    if provider == "anthropic":
        # Anthropic uses own API (not OpenAI-compat). This URL is used only for display.
        return "https://api.anthropic.com"
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "deepseek":
        return "https://api.deepseek.com"
    if provider == "gemini":
        return "https://generativelanguage.googleapis.com/openai"
    if provider == "mistral":
        return "https://api.mistral.ai/v1"
    if provider == "groq":
        return "https://api.groq.com/openai/v1"
    if provider == "together":
        return "https://api.together.xyz/v1"
    if provider == "fireworks":
        return "https://api.fireworks.ai/inference/v1"
    if provider == "xai":
        return "https://api.x.ai/v1"
    if provider == "cohere":
        return "https://api.cohere.com/compatibility/v1"
    if provider == "perplexity":
        return "https://api.perplexity.ai"
    if provider == "minimax":
        return "https://api.minimax.io/v1"
    if provider == "kimi":
        return "https://api.moonshot.cn/v1"
    if provider == "qwen":
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # Fallback: ollama via OpenAI-compatible (Ollama 0.1.24+)
    return str(settings.ollama_url)


def _provider_api_key(provider: str) -> str:
    """Return API key for the given cloud provider (empty string for local)."""
    import os

    key_map = {
        "openrouter": settings.openrouter_api_key,
        "anthropic": settings.anthropic_api_key,
        "deepseek": settings.deepseek_api_key,
        "openai": os.environ.get("OPENAI_API_KEY", ""),
        "gemini": os.environ.get("GEMINI_API_KEY", ""),
        "mistral": os.environ.get("MISTRAL_API_KEY", ""),
        "groq": os.environ.get("GROQ_API_KEY", ""),
        "together": os.environ.get("TOGETHER_API_KEY", ""),
        "fireworks": os.environ.get("FIREWORKS_API_KEY", ""),
        "xai": os.environ.get("XAI_API_KEY", ""),
        "cohere": os.environ.get("COHERE_API_KEY", ""),
        "perplexity": os.environ.get("PERPLEXITY_API_KEY", ""),
        "minimax": os.environ.get("MINIMAX_API_KEY", ""),
        "kimi": os.environ.get("MOONSHOT_API_KEY", ""),
        "qwen": os.environ.get("DASHSCOPE_API_KEY", ""),
        "openai_compatible": os.environ.get("OPENAI_COMPATIBLE_API_KEY", ""),
        "vllm": os.environ.get("VLLM_API_KEY", ""),
        "lmstudio": os.environ.get("LMSTUDIO_API_KEY", ""),
    }
    return key_map.get(provider, "")
