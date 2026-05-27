"""Unified model+provider resolution for all AI tasks.

Single source of truth: all code that needs to know "which model and which
provider to use for OCR / VLM / reasoning / verify" should call this module.
Settings hierarchy (highest to lowest priority):
  1. ai_config in Redis/file (saved from the Settings → Models UI)
  2. Environment variables / pydantic Settings defaults
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass

from app.config import settings

logger = structlog.get_logger()


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


def _get_ai_config() -> dict:
    try:
        from app.api.ai_settings import get_ai_config
        return get_ai_config()
    except Exception as exc:
        logger.warning("model_resolver_config_unavailable", error=str(exc))
        return {}


def get_ocr_model() -> ModelConfig:
    """Model for OCR / invoice extraction. Must be local (documents are confidential)."""
    cfg = _get_ai_config()
    model = (cfg.get("model_ocr") or "").strip() or settings.ollama_model_ocr
    provider = (cfg.get("model_ocr_provider") or "").strip() or "ollama"
    # Safety: OCR is always local-only
    if provider not in ("ollama", "llamacpp", "vllm", "lmstudio", "openai_compatible"):
        logger.warning("model_resolver_ocr_cloud_blocked", provider=provider, model=model)
        provider = "ollama"
    return ModelConfig(model=model, provider=provider)


def get_vlm_model() -> ModelConfig:
    """Vision Language Model for drawing / image analysis. Must be local."""
    cfg = _get_ai_config()
    model = (cfg.get("model_vlm") or "").strip() or settings.ollama_model_vlm
    provider = (cfg.get("model_vlm_provider") or "").strip() or "ollama"
    # Safety: VLM (drawings) is always local-only
    if provider not in ("ollama", "llamacpp", "vllm", "lmstudio", "openai_compatible"):
        logger.warning("model_resolver_vlm_cloud_blocked", provider=provider, model=model)
        provider = "ollama"
    return ModelConfig(model=model, provider=provider)


def get_reasoning_model(confidential: bool = False) -> ModelConfig:
    """Model for reasoning tasks. Cloud providers allowed when confidential=False."""
    cfg = _get_ai_config()
    model = (cfg.get("model_reasoning") or "").strip() or settings.ollama_model_reasoning
    provider = (cfg.get("model_reasoning_provider") or "").strip() or "ollama"
    # Enforce local-only for confidential tasks
    if confidential and provider not in ("ollama", "llamacpp", "vllm", "lmstudio", "openai_compatible"):
        logger.warning(
            "model_resolver_reasoning_cloud_blocked_confidential",
            provider=provider, model=model,
        )
        provider = "ollama"
        model = settings.ollama_model_reasoning
    return ModelConfig(model=model, provider=provider)


def get_verify_model() -> ModelConfig:
    """Model for extraction verification. Must be local."""
    cfg = _get_ai_config()
    model = (cfg.get("verify_model_1") or "").strip() or settings.ollama_model_ocr
    provider = (cfg.get("verify_model_1_provider") or "").strip() or "ollama"
    if provider not in ("ollama", "llamacpp", "vllm", "lmstudio", "openai_compatible"):
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
