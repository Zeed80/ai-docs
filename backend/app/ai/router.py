"""AI Router — single entry point for all AI calls.

Business code must import only from this module, not from ollama_client directly.
This allows swapping models and backends without touching business logic.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

from app.ai.extraction_prompts import (
    CLASSIFY_PROMPT,
    CLASSIFY_SYSTEM,
    EXTRACT_INVOICE_PROMPT,
    EXTRACT_INVOICE_SYSTEM,
    SUMMARIZE_PROMPT,
    SUMMARIZE_SYSTEM,
)
from app.ai.model_registry import ModelRegistry
from app.ai.ollama_client import generate_json, reasoning_generate
from app.ai.providers.anthropic_provider import AnthropicProvider
from app.ai.providers.base import AIProvider
from app.ai.providers.ollama import OllamaProvider
from app.ai.providers.openai_compatible import OpenAICompatibleProvider
from app.ai.providers.openrouter import OpenRouterProvider
from app.ai.providers.vllm import VLLMProvider
from app.ai.schemas import (
    AIRequest,
    AIResponse,
    AITask,
    ChatMessage,
    ModelCapability,
    ProviderKind,
)
from app.config import settings

logger = structlog.get_logger()


class AIConfidentialityPolicyError(RuntimeError):
    """Raised when an AI request would violate local-only/confidential policy."""

_EMAIL_SYSTEM = """Ты — AI-сотрудник Света. Пишешь деловые письма на русском языке
для промышленного предприятия. Отвечай строго в JSON."""

_EMAIL_PROMPT = """Составь деловое письмо по следующему контексту:

{context_json}

Ответь в JSON:
{{
  "subject": "<тема письма>",
  "body_text": "<тело письма в plain text>",
  "body_html": "<тело письма в HTML>",
  "tone": "formal",
  "risk_flags": []
}}"""

_NL_QUERY_SYSTEM = """Ты — SQL-ассистент. Преобразуй запрос на естественном языке
в структурированный JSON-фильтр. Отвечай строго в JSON."""

_NL_QUERY_PROMPT = """Преобразуй запрос в структурированный фильтр.
Схема данных: {schema_json}

Запрос: {nl_text}

Ответь в JSON:
{{
  "filters": {{"<field>": "<value>"}},
  "sort_by": "<field or null>",
  "sort_order": "desc",
  "limit": 50
}}"""

_CHAT_TITLE_SYSTEM = """Ты называешь чаты в интерфейсе AI-сотрудника.
Ответь только коротким названием на русском языке без кавычек и пояснений."""

_CHAT_TITLE_PROMPT = """Сформулируй краткое название чата по первому запросу пользователя.

Требования:
- 2-5 слов
- до 45 символов
- без точки в конце
- без кавычек
- отражай основной смысл запроса

Запрос:
{message}

Название:"""


def _runtime_ocr_model() -> str:
    """Resolve OCR/extraction model name from runtime AI config (legacy helper)."""
    from app.ai.model_resolver import get_ocr_model
    return get_ocr_model().model


def _runtime_ocr_model_and_provider() -> tuple[str, str]:
    """Resolve OCR/extraction model + provider from runtime AI config."""
    from app.ai.model_resolver import get_ocr_model
    cfg = get_ocr_model()
    return cfg.model, cfg.provider


def _clean_chat_title(title: str, *, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "").strip().strip("\"'«»“”"))
    cleaned = re.sub(r"[.!?…]+$", "", cleaned).strip()
    if not cleaned or len(cleaned) > 80:
        cleaned = _fallback_chat_title(fallback)
    if len(cleaned) > 45:
        cleaned = cleaned[:45].rsplit(" ", 1)[0].strip() or cleaned[:45].strip()
    return cleaned or "Новый чат"


def _fallback_chat_title(first_message: str) -> str:
    text = re.sub(r"\s+", " ", (first_message or "").strip())
    if not text:
        return "Новый чат"
    text = re.sub(
        r"^(пожалуйста|света|выведи|покажи|сделай|напиши|расскажи|найди)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    words = text.split()[:5]
    return " ".join(words)[:45].strip(" ,.;:!?") or "Новый чат"


class AIRouter:
    """Unified AI router — routes tasks to appropriate models.

    The legacy convenience methods below are kept for existing invoice/document
    code. New code should call ``run(AIRequest(...))`` so model choice,
    local-only policy, structured output validation, and tool allowlisting stay
    in one place.
    """

    def __init__(
        self,
        registry: ModelRegistry | None = None,
        providers: dict[ProviderKind, AIProvider] | None = None,
    ) -> None:
        self.registry = registry or ModelRegistry.from_yaml(
            "backend/app/ai/config/model_registry.yaml"
        )
        self.providers = providers or {
            kind: self._provider_from_config(kind)
            for kind in self.registry.providers
        }

    def _provider_from_config(self, kind: ProviderKind) -> AIProvider:
        config = self.registry.providers[kind]
        if kind == ProviderKind.OLLAMA:
            config = config.model_copy(update={"base_url": settings.ollama_url})
            return OllamaProvider(config)
        if kind == ProviderKind.VLLM:
            # Use the runtime VLLM_URL (e.g. http://vllm-server:8000) rather than
            # the static registry base_url, mirroring ollama/llamacpp.
            import os
            vllm_url = os.environ.get("VLLM_URL", "").strip()
            if vllm_url:
                config = config.model_copy(update={"base_url": vllm_url})
            return VLLMProvider(config)
        if kind == ProviderKind.LLAMACPP:
            # llama-server speaks OpenAI-compatible API; use runtime URL from settings
            config = config.model_copy(update={"base_url": settings.llamacpp_url})
            return OpenAICompatibleProvider(config)
        if kind in (ProviderKind.OPENAI_COMPATIBLE, ProviderKind.CLOUD_PROVIDER):
            return OpenAICompatibleProvider(config)
        if kind == ProviderKind.ANTHROPIC:
            return AnthropicProvider.from_env()
        if kind == ProviderKind.OPENROUTER:
            return OpenRouterProvider.from_env()
        if kind in (ProviderKind.DEEPSEEK, ProviderKind.GEMINI):
            # OpenAI-compatible endpoints — use base_url from registry, key from env
            return OpenAICompatibleProvider(config)
        raise KeyError(f"Unsupported AI provider: {kind.value}")

    async def run(self, request: AIRequest) -> AIResponse:
        """Run one AI task through the routing config and validate the result.

        Candidate models and the local/cloud policy come from ``task_routing``
        (the single source of truth, editable from the UI), not from the static
        YAML ``fallback_chain``. The per-call ``confidential`` flag can only
        tighten the configured policy, never loosen it.
        """
        from app.ai.task_routing import get_routing_for

        routing = get_routing_for(request.task)
        candidates = [request.preferred_model] if request.preferred_model else routing.models

        # Merge per-call request with the task's configured policy.
        eff_confidential = request.confidential or routing.local_only
        eff_allow_cloud = (not eff_confidential) and (request.allow_cloud or routing.allow_cloud)
        if (eff_confidential, eff_allow_cloud) != (request.confidential, request.allow_cloud):
            request = request.model_copy(
                update={"confidential": eff_confidential, "allow_cloud": eff_allow_cloud}
            )

        last_error: Exception | None = None

        import time as _time

        for model_name in [name for name in candidates if name]:
            model = self.registry.get_model(model_name)
            self._enforce_policy(request, model)  # policy errors are not per-model failures
            provider = self.providers[model.provider]
            # Container-bound servers (vLLM/llama.cpp) are started on demand and
            # auto-stopped when idle; bring the target up before dispatching.
            try:
                from app.ai import server_lifecycle

                await server_lifecycle.ensure_running(model.provider.value)
            except Exception as exc:
                logger.debug("ensure_server_running_failed", model=model_name, error=str(exc))
            started = _time.perf_counter()
            try:
                response = await self._dispatch(provider, request, model)
                response = self._validate_structured_output(request, response)
                response.proposed_tool_calls = self._filter_tool_calls(request, response)
                self._record_telemetry(request, model, started, ok=True, response=response)
                return response
            except Exception as exc:
                last_error = exc
                self._record_telemetry(request, model, started, ok=False, error=str(exc))
                logger.warning(
                    "ai_route_model_failed",
                    task=request.task.value,
                    model=model_name,
                    error=str(exc),
                )

        if last_error:
            raise last_error
        raise KeyError(f"No model configured for task {request.task.value}")

    def _record_telemetry(
        self,
        request: AIRequest,
        model: ModelCapability,
        started: float,
        *,
        ok: bool,
        response: AIResponse | None = None,
        error: str | None = None,
    ) -> None:
        import time as _time

        try:
            from app.ai import telemetry

            usage = response.usage if response else None
            telemetry.record_call(
                task=request.task.value,
                model=model.name,
                provider=model.provider.value,
                latency_ms=int((_time.perf_counter() - started) * 1000),
                ok=ok,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                error=error,
            )
        except Exception:
            pass

    def _enforce_policy(self, request: AIRequest, model: ModelCapability) -> None:
        provider = self.registry.providers[model.provider]
        cloud_requested = not provider.is_local or not model.local_only
        if request.confidential and cloud_requested:
            raise AIConfidentialityPolicyError(
                f"Confidential task {request.task.value} cannot use non-local model {model.name}"
            )
        if cloud_requested and not request.allow_cloud:
            raise AIConfidentialityPolicyError(
                f"Cloud model {model.name} requires allow_cloud=True"
            )

    async def _dispatch(
        self,
        provider: AIProvider,
        request: AIRequest,
        model: ModelCapability,
    ) -> AIResponse:
        provider_model = model.provider_model
        # Inject inference parameters from profile unless caller explicitly set them
        if not request.metadata.get("inference_params"):
            try:
                from app.ai.parameter_profiles import get_inference_params
                params = get_inference_params(request.task)
                request = request.model_copy(
                    update={"metadata": {**request.metadata, "inference_params": params}}
                )
            except Exception:
                pass

        if request.task == AITask.EMBEDDING:
            return await provider.embedding(request, provider_model)
        if request.task == AITask.RERANKING:
            return await provider.rerank(request, provider_model)
        if request.task == AITask.SPEECH:
            return await provider.speech(request, provider_model)
        if request.task == AITask.TOOL_CALLING:
            return await provider.tool_calling(request, provider_model)
        # Vision only when images are actually supplied — a model having the
        # vision modality must still serve text-only requests via chat.
        if request.images:
            return await provider.vision(request, provider_model)
        if request.response_schema is not None:
            return await provider.structured_extract(request, provider_model)
        return await provider.chat(request, provider_model)

    def _validate_structured_output(self, request: AIRequest, response: AIResponse) -> AIResponse:
        schema = request.response_schema
        if schema is None:
            return response
        if schema and isinstance(response.data, schema):
            return response

        # Try Pydantic validation on data first
        payload: Any = response.data
        if payload is None:
            # Fall back to structured_output extractor for weak model text responses
            from app.ai.structured_output import parse_json_output
            text = response.text or "{}"
            payload = parse_json_output(text)
            if payload is None:
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = {}

        try:
            response.data = schema.model_validate(payload)
        except Exception as exc:
            logger.warning(
                "structured_output_validation_failed",
                schema=schema.__name__ if hasattr(schema, "__name__") else str(schema),
                error=str(exc),
            )
        return response

    def _filter_tool_calls(
        self,
        request: AIRequest,
        response: AIResponse,
    ):
        if not request.tools:
            return []
        allowed = {tool.name for tool in request.tools}
        return [call for call in response.proposed_tool_calls if call.name in allowed]

    def _ocr_model_and_provider(self) -> tuple[str, str]:
        """Return (model, provider) for OCR tasks from ai_config."""
        from app.ai.model_resolver import get_ocr_model
        cfg = get_ocr_model()
        return cfg.model, cfg.provider

    def _ocr_temperature(self, task: AITask = AITask.INVOICE_OCR) -> float:
        """Return temperature for OCR/classification tasks from the active profile."""
        try:
            from app.ai.parameter_profiles import get_inference_params
            return float(get_inference_params(task).get("temperature", 0.0))
        except Exception:
            return 0.0

    async def extract_invoice(self, text: str) -> dict:
        model, provider = self._ocr_model_and_provider()
        logger.info("extract_invoice_model", model=model, provider=provider)
        prompt = EXTRACT_INVOICE_PROMPT.format(text=text[:8000])
        return await generate_json(
            prompt,
            model=model,
            provider=provider,
            system=EXTRACT_INVOICE_SYSTEM,
            temperature=self._ocr_temperature(AITask.INVOICE_OCR),
            max_tokens=8192,
            timeout_seconds=180.0,
        )

    async def classify_document(self, text: str) -> dict:
        model, provider = self._ocr_model_and_provider()
        logger.info("classify_document_model", model=model, provider=provider)
        prompt = CLASSIFY_PROMPT.format(text=text[:3000])
        return await generate_json(
            prompt,
            model=model,
            provider=provider,
            system=CLASSIFY_SYSTEM,
            temperature=self._ocr_temperature(AITask.CLASSIFICATION),
        )

    async def summarize_document(self, text: str) -> dict:
        model, provider = self._ocr_model_and_provider()
        prompt = SUMMARIZE_PROMPT.format(text=text[:4000])
        return await generate_json(
            prompt,
            model=model,
            provider=provider,
            system=SUMMARIZE_SYSTEM,
            temperature=self._ocr_temperature(AITask.LONG_CONTEXT_SUMMARIZATION),
        )

    async def generate_email(self, context: dict) -> dict:
        import json
        prompt = _EMAIL_PROMPT.format(context_json=json.dumps(context, ensure_ascii=False))
        raw = await reasoning_generate(
            prompt,
            system=_EMAIL_SYSTEM,
            format_json=True,
            confidential=True,
        )
        import json as _json
        try:
            return _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return {"subject": "", "body_text": raw, "body_html": None, "risk_flags": []}

    async def generate_chat_title(self, first_message: str) -> str:
        response = await self.run(
            AIRequest(
                task=AITask.CLASSIFICATION,
                messages=[
                    ChatMessage(role="system", content=_CHAT_TITLE_SYSTEM),
                    ChatMessage(
                        role="user",
                        content=_CHAT_TITLE_PROMPT.format(message=first_message[:1500]),
                    ),
                ],
                confidential=True,
                allow_cloud=False,
            )
        )
        return _clean_chat_title(response.text or "", fallback=first_message)

    async def analyze_email_style(self, emails_text: str, count: int) -> dict:
        from app.ai.model_resolver import get_ocr_model
        ocr_cfg = get_ocr_model()
        system = """You are a communication style analyzer for business emails.
Analyze the writing style of emails and provide recommendations. Respond in JSON only."""
        prompt = f"""Analyze the writing style of these {count} emails:

{emails_text[:3000]}

Respond with JSON:
{{
  "tone": "formal|friendly|neutral",
  "language": "ru|en|mixed",
  "greeting_style": "<typical greeting>",
  "closing_style": "<typical closing>",
  "avg_length": <average word count>,
  "recommendations": ["<recommendation 1>", "<recommendation 2>"]
}}"""
        return await generate_json(
            prompt,
            model=ocr_cfg.model,
            provider=ocr_cfg.provider,
            system=system,
            timeout_seconds=30.0,
        )

    async def nl_to_query(self, nl: str, schema: dict | None = None) -> dict:
        import json
        prompt = _NL_QUERY_PROMPT.format(
            nl_text=nl,
            schema_json=json.dumps(schema or {}, ensure_ascii=False),
        )
        raw = await reasoning_generate(prompt, system=_NL_QUERY_SYSTEM, format_json=True)
        import json as _json
        try:
            return _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return {"filters": {}, "sort_by": None, "sort_order": "desc", "limit": 50}


ai_router = AIRouter()
