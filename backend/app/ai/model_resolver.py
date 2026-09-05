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

from dataclasses import dataclass

import structlog

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


def _force_local(
    task: AITask, model: str, provider: str, local_model: str, event: str
) -> ModelConfig:
    """Отвести конфиденциальную задачу на локальную модель — пару целиком.

    Раньше подменялся только провайдер, а имя модели оставалось облачным: в
    локальный Ollama уходил запрос вида `claude-sonnet-5` и возвращал 404, то
    есть задача не «падала на локальную модель», а просто ломалась. Провайдер
    и имя должны меняться вместе — иначе получается пара, которой нет нигде.
    """
    logger.error(
        event,
        task=task.value,
        blocked_provider=provider,
        blocked_model=model,
        used_model=local_model,
    )
    return ModelConfig(model=local_model, provider="ollama")


def get_ocr_model() -> ModelConfig:
    """Model for OCR / invoice extraction. Must be local (documents are confidential)."""
    model, provider = _resolve(AITask.INVOICE_OCR, settings.ollama_model_ocr)
    if provider not in _LOCAL_PROVIDERS:
        return _force_local(
            AITask.INVOICE_OCR,
            model,
            provider,
            settings.ollama_model_ocr,
            "model_resolver_ocr_cloud_blocked",
        )
    return ModelConfig(model=model, provider=provider)


def get_vlm_model() -> ModelConfig:
    """Vision Language Model for drawing / image analysis. Must be local."""
    model, provider = _resolve(AITask.DRAWING_ANALYSIS_VLM, settings.ollama_model_vlm)
    if provider not in _LOCAL_PROVIDERS:
        return _force_local(
            AITask.DRAWING_ANALYSIS_VLM,
            model,
            provider,
            settings.ollama_model_vlm,
            "model_resolver_vlm_cloud_blocked",
        )
    return ModelConfig(model=model, provider=provider)


def get_reasoning_model(confidential: bool = False) -> ModelConfig:
    """Model for reasoning tasks. Cloud providers allowed when confidential=False."""
    model, provider = _resolve(AITask.ENGINEERING_REASONING, settings.ollama_model_reasoning)
    # Enforce local-only for confidential tasks
    if confidential and provider not in _LOCAL_PROVIDERS:
        logger.warning(
            "model_resolver_reasoning_cloud_blocked_confidential",
            provider=provider,
            model=model,
        )
        provider = "ollama"
        model = settings.ollama_model_reasoning
    return ModelConfig(model=model, provider=provider)


def get_verify_model() -> ModelConfig:
    """Model for extraction verification. Must be local."""
    model, provider = _resolve(AITask.STRUCTURED_EXTRACTION, settings.ollama_model_ocr)
    if provider not in _LOCAL_PROVIDERS:
        return _force_local(
            AITask.STRUCTURED_EXTRACTION,
            model,
            provider,
            settings.ollama_model_ocr,
            "model_resolver_verify_cloud_blocked",
        )
    return ModelConfig(model=model, provider=provider)


# ---------------------------------------------------------------------------
# Provider URL helpers (used by dispatch functions)
# ---------------------------------------------------------------------------

# Legacy-псевдонимы: в каталоге и в вызывающем коде исторически встречаются
# «kimi» и «qwen», а в ProviderKind те же провайдеры называются moonshot и
# dashscope. Из-за этого реестр их не резолвил, и они уходили в захардкоженную
# таблицу — где адрес успел устареть (api.moonshot.cn против api.moonshot.ai,
# dashscope без -intl).
_PROVIDER_ALIASES = {
    "kimi": "moonshot",
    "qwen": "dashscope",
}


def _resolved_node(provider: str):
    """Узел из provider_instances для этого kind.

    Единственный источник адреса и ключа. Раньше рядом жила таблица из 18
    захардкоженных URL и свой набор env-имён: узел, заведённый в GUI (второй
    Ollama на другой машине, свой base_url, ключ из зашифрованного хранилища),
    на прямой путь вызова не действовал. Хуже того, таблица разошлась с
    реестром по пяти адресам сразу — она просто устарела и никто этого не
    замечал, потому что оба места «работали».

    Сам реестр многоуровневый: строка БД → YAML → env, — поэтому отдельный
    резерв здесь не нужен даже когда Redis или Postgres недоступны.
    """
    kind_value = _PROVIDER_ALIASES.get(provider, provider)
    try:
        from app.ai.provider_registry import select_instance
        from app.ai.schemas import ProviderKind

        return select_instance(ProviderKind(kind_value))
    except ValueError:
        # Незнакомый провайдер: молча подставлять чужой адрес нельзя — так
        # рождается пара «облачное имя модели на локальном узле», которая
        # падает 404 и выглядит как поломка самой задачи.
        logger.error("model_resolver_unknown_provider", provider=provider)
        return None
    except Exception as exc:  # noqa: BLE001 — реестр недоступен целиком
        logger.error("model_resolver_node_lookup_failed", provider=provider, error=str(exc))
        return None


def _provider_base_url(provider: str) -> str:
    """Базовый адрес провайдера (без /v1 — потребители дописывают его сами)."""
    node = _resolved_node(provider)
    if node is None or not node.base_url:
        return ""
    return str(node.base_url).rstrip("/").removesuffix("/v1")


def _provider_api_key(provider: str) -> str:
    """Ключ провайдера; пустая строка для локальных и для незаданного ключа."""
    node = _resolved_node(provider)
    return str(node.api_key) if node is not None and node.api_key else ""
