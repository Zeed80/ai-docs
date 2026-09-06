from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

from app.ai.providers.base import AIProvider
from app.ai.schemas import (
    AIRequest,
    AIResponse,
    AIUsage,
    ChatMessage,
    ProposedToolCall,
    ProviderKind,
)

_VERSIONED_TAIL = re.compile(r"/(v\d+[a-z]*|openai)$")


def openai_endpoint(base_url: object, path: str) -> str:
    """Собрать URL эндпоинта, не задваивая версию.

    Локальные серверы задаются голым адресом (``http://host:11436``) — им ``/v1``
    дописать надо. Облачные шлюзы почти все уже несут версию в base_url:
    ``https://openrouter.ai/api/v1``, ``https://api.groq.com/openai/v1``,
    ``https://dashscope-intl.aliyuncs.com/compatible-mode/v1``,
    ``https://generativelanguage.googleapis.com/v1beta/openai``. Безусловный
    ``/v1`` давал им ``…/api/v1/v1/chat/completions`` и 404 — то есть ни одна
    облачная модель через этот класс не работала. Ответ при этом приходил:
    роутер тихо уходил на локальный фолбэк, и со стороны человека облачная
    модель выглядела назначенной и рабочей.
    """
    base = str(base_url).rstrip("/")
    if _VERSIONED_TAIL.search(base):
        return f"{base}/{path.lstrip('/')}"
    return f"{base}/v1/{path.lstrip('/')}"


def _thinking_params(request: AIRequest, provider_kind: str) -> dict[str, Any]:
    """Reasoning/CoT HTTP params for this request, or {} if undecided.

    ``request.thinking`` is ``None`` only when a caller bypasses AIRouter's
    resolution (AIRouter.run always resolves it to a concrete bool before
    dispatching). Stay silent in that case rather than force an explicit
    "off" onto a request nobody made a decision about — this is the gap fix
    for the AIRouter path: previously thinking was never read here at all.
    """
    if request.thinking is None:
        return {}
    from app.ai.thinking_params import thinking_request_params

    return thinking_request_params(provider_kind, request.thinking, request.thinking_level)


from app.ai.output_format import (
    FormatMechanism,
    contract_of,
    inline_schema_defs,
    schema_hint_text,
    strictify_schema,
)


def _requested_schema(request: AIRequest) -> dict[str, Any] | None:
    """JSON-схема, которую просит вызывающий: сырая из metadata или из модели."""
    schema = (request.metadata or {}).get("json_schema")
    if schema is None and request.response_schema is not None:
        try:
            schema = request.response_schema.model_json_schema()
        except Exception:  # noqa: BLE001 — схема необязательна, JSON важнее
            schema = None
    return schema if isinstance(schema, dict) else None


def _schema_hint(request: AIRequest) -> str:
    """Схема словами — для модели, которая не умеет строгий структурный вывод.

    Когда провайдер подтверждает ``structured_outputs``, схема уходит в
    ``response_format`` и соблюдается принудительно. Когда нет, остаётся один
    способ сообщить форму — сказать её в тексте. Без этого модель отвечала
    валидным, но чужим JSON: читатель чертежа ждёт ``{"frames": [...]}``, а
    приходил голый массив, и слой PMI терялся целиком.
    """
    contract = contract_of(request)
    if contract is not None:
        return schema_hint_text(contract)
    if (request.metadata or {}).get("structured_output_supported"):
        return ""
    schema = _requested_schema(request)
    if not schema:
        return ""
    import json as _json

    return (
        "\n\nОтветь ОДНИМ объектом JSON строго по этой схеме, без пояснений и "
        "без markdown-ограждений:\n" + _json.dumps(schema, ensure_ascii=False)
    )


def _response_format(request: AIRequest) -> dict[str, Any]:
    """Требование структурированного ответа для OpenAI-совместимого шлюза.

    Найдено на живом чтении чертежа: облачная модель прекрасно ВИДЕЛА лист —
    узнала ступенчатый вал, обозначение, сталь 45, резьбы M18×1.5 и M24×1.5 —
    но отвечала markdown-отчётом на английском, потому что схему ей никто не
    передавал. Ollama-провайдер кладёт её в ``format``, а здесь и
    ``response_schema``, и ``metadata["json_schema"]`` просто игнорировались:
    все 29 запросов чтения были отброшены как «не JSON», и деталь осталась
    неопознанной. Просьба «ответь одной строкой JSON» в тексте промпта — не
    ограничение, а пожелание.

    Строгую схему принимает не каждая модель (у OpenRouter это отдельный
    параметр ``structured_outputs``, и у бесплатных вариантов его обычно нет),
    поэтому без подтверждённой поддержки просим просто валидный JSON: этого
    достаточно, чтобы ответ разобрался, а схему проверит вызывающий.
    """
    contract = contract_of(request)
    if contract is not None:
        if contract.mechanism is FormatMechanism.NATIVE_SCHEMA and contract.schema:
            # `strict: true` требует закрытых объектов и всех свойств в
            # `required`; схема Pydantic этому не отвечает, и без подготовки
            # строгий режим просто возвращает 400.
            prepared = strictify_schema(inline_schema_defs(contract.schema))
            return {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": contract.schema_name,
                        "strict": True,
                        "schema": prepared,
                    },
                }
            }
        if contract.mechanism is FormatMechanism.JSON_MODE:
            return {"response_format": {"type": "json_object"}}
        return {}

    meta = request.metadata or {}
    schema = _requested_schema(request)
    if not schema and request.response_schema is None:
        return {}
    if meta.get("structured_output_supported") and isinstance(schema, dict):
        return {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": schema},
            }
        }
    return {"response_format": {"type": "json_object"}}


# Шлюзы, которые принимают НЕстандартные для OpenAI сэмплирующие параметры.
# `top_k`/`min_p` в спецификации OpenAI отсутствуют: локальные серверы их
# принимают как расширение, а строгий облачный шлюз отвечает на них 400 —
# то есть параметр, отправленный «на всякий случай», стоит целого кандидата.
_EXTRA_SAMPLING_KINDS = frozenset(
    {"vllm", "llamacpp", "openai_compatible", "lmstudio", "openrouter"}
)


def _inference_params(
    request: AIRequest,
    default_temperature: float = 0.2,
    *,
    provider_kind: str | None = None,
) -> dict[str, Any]:
    """Extract inference parameters from request metadata."""
    params = (request.metadata or {}).get("inference_params") or {}
    result: dict[str, Any] = {"temperature": params.get("temperature", default_temperature)}
    # Cap output length. Unlike Ollama (which generates freely by default), an
    # OpenAI-compatible server left without max_tokens can truncate the response
    # to a handful of tokens — a structured-JSON read (spec / graph) then comes
    # back as an unparseable stub. Honor an explicit budget, else a generous
    # default big enough for a full spec.
    max_tokens = params.get("max_tokens") or (request.metadata or {}).get("num_predict") or 4096
    result["max_tokens"] = int(max_tokens)
    if "top_p" in params:
        result["top_p"] = params["top_p"]
    if provider_kind is None or provider_kind in _EXTRA_SAMPLING_KINDS:
        if "top_k" in params:
            result["top_k"] = params["top_k"]
        if "min_p" in params:
            result["min_p"] = params["min_p"]
    if "repeat_penalty" in params:
        result["frequency_penalty"] = (
            params["repeat_penalty"] - 1.0
        )  # OpenAI uses frequency_penalty
    return result


class OpenAICompatibleProvider(AIProvider):
    kind = ProviderKind.OPENAI_COMPATIBLE

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.config.extra_headers}
        # Prefer the runtime-resolved key (provider_instances DB row), then env.
        api_key = self.config.api_key or (
            os.getenv(self.config.api_key_env) if self.config.api_key_env else None
        )
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _messages(self, request: AIRequest) -> list[dict[str, Any]]:
        if request.messages:
            messages = [message.model_dump() for message in request.messages]
        elif request.prompt:
            messages = [ChatMessage(role="user", content=request.prompt).model_dump()]
        elif request.input_text:
            messages = [ChatMessage(role="user", content=request.input_text).model_dump()]
        else:
            return []
        hint = _schema_hint(request)
        if hint:
            for message in reversed(messages):
                if message.get("role") == "user":
                    message["content"] = f"{message.get('content') or ''}{hint}"
                    break
        return messages

    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._messages(request),
            **_inference_params(
                request, default_temperature=0.2, provider_kind=self.config.kind.value
            ),
            **_thinking_params(request, self.config.kind.value),
            **_response_format(request),
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                openai_endpoint(self.config.base_url, "chat/completions"),
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            body = response.json()

        choice = body.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = body.get("usage", {})
        tool_calls = []
        for call in message.get("tool_calls", []) or []:
            function = call.get("function", {})
            tool_calls.append(
                ProposedToolCall(
                    name=function.get("name", ""),
                    arguments=_parse_json_object(function.get("arguments")),
                )
            )

        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            text=message.get("content"),
            proposed_tool_calls=tool_calls,
            usage=AIUsage(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                latency_ms=int((time.perf_counter() - started) * 1000),
            ),
            raw=body,
        )

    async def vision(self, request: AIRequest, model: str) -> AIResponse:
        started = time.perf_counter()
        content: list[dict[str, Any]] = []
        for image in request.images:
            # request.images carry raw base64 PNG (the Ollama convention, which is
            # lenient). Strict OpenAI-compatible servers (vLLM, llama.cpp) require a
            # real URL — reject raw base64 with "URL must be HTTP, data or file URL"
            # — so wrap it in a data URI unless it already is one.
            url = (
                image
                if image.startswith(("data:", "http://", "https://", "file://"))
                else f"data:image/png;base64,{image}"
            )
            content.append({"type": "image_url", "image_url": {"url": url}})
        # Вопрос к картинке брался ТОЛЬКО из `prompt`/`input_text`, а читатель
        # чертежа задаёт его через `messages` — как и всё остальное в проекте.
        # Для облачной модели это значило пустой текст: она получала лист без
        # единого вопроса и отвечала вольным описанием, в разной раскладке и на
        # разном языке. Снаружи выглядело как «умная модель не смогла даже тип
        # детали определить» — при том что её ни о чём не спросили. Ollama-путь
        # читает messages с самого начала, поэтому дефект жил только в облаке.
        system_text = ""
        user_parts: list[str] = []
        for message in request.messages:
            if message.role == "system":
                system_text = message.content
            elif message.role in ("user", "assistant"):
                user_parts.append(message.content)
        question = "\n\n".join(user_parts) or request.prompt or request.input_text or ""
        content.append({"type": "text", "text": question + _schema_hint(request)})
        messages: list[dict[str, Any]] = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": content})
        payload = {
            "model": model,
            "messages": messages,
            **_inference_params(
                request, default_temperature=0.0, provider_kind=self.config.kind.value
            ),
            **_thinking_params(request, self.config.kind.value),
            **_response_format(request),
        }
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                openai_endpoint(self.config.base_url, "chat/completions"),
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        text = body.get("choices", [{}])[0].get("message", {}).get("content")
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            text=text,
            usage=AIUsage(latency_ms=int((time.perf_counter() - started) * 1000)),
            raw=body,
        )

    async def embedding(self, request: AIRequest, model: str) -> AIResponse:
        started = time.perf_counter()
        payload = {"model": model, "input": request.input_text or request.prompt or ""}
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                openai_endpoint(self.config.base_url, "embeddings"),
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        embedding = body.get("data", [{}])[0].get("embedding", [])
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            embedding=embedding,
            usage=AIUsage(latency_ms=int((time.perf_counter() - started) * 1000)),
            raw=body,
        )

    async def rerank(self, request: AIRequest, model: str) -> AIResponse:
        started = time.perf_counter()
        documents = request.metadata.get("documents") or []
        payload = {
            "model": model,
            "query": request.input_text or request.prompt or "",
            "documents": documents,
        }
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                openai_endpoint(self.config.base_url, "rerank"),
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        results = body.get("results") or []
        scores = [item.get("relevance_score", item.get("score", 0.0)) for item in results]
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            scores=scores,
            usage=AIUsage(latency_ms=int((time.perf_counter() - started) * 1000)),
            raw=body,
        )


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    import json

    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}
