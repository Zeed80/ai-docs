"""Anthropic provider for the AIRouter (structured extraction, reasoning).

Uses the Anthropic Messages API directly via httpx so that the `anthropic`
SDK is optional.  Prompt caching is injected via the
``anthropic-beta: prompt-caching-2024-07-31`` header.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from app.ai.output_format import (
    FormatMechanism,
    contract_of,
    inline_schema_defs,
    schema_hint_text,
)
from app.ai.providers.base import AIProvider
from app.ai.schemas import (
    AIRequest,
    AIResponse,
    AIUsage,
    ProposedToolCall,
    ProviderConfig,
    ProviderKind,
)

_ANTHROPIC_API = "https://api.anthropic.com/v1"
_ANTHROPIC_VERSION = "2023-06-01"
_MAX_TOKENS = 4096
# Anthropic's own ceiling on the current generation. Clamping here beats a 400
# from the wire when a caller asks for more than the model can produce.
_MAX_TOKENS_CEILING = 128000


def _resolve_max_tokens(request: AIRequest) -> int:
    """Output ceiling the CALLER asked for, not a constant.

    ``max_tokens`` was hardcoded at 4096 while the drawing reader asks for 6000
    and 8000 — every structured read through Claude was truncated mid-JSON and
    counted as a failed pass. Both channels the rest of the project uses are
    honoured: ``inference_params.max_tokens`` and ``metadata["num_predict"]``.
    """
    meta = request.metadata or {}
    params = meta.get("inference_params") or {}
    requested = params.get("max_tokens") or meta.get("num_predict")
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return _MAX_TOKENS
    return max(256, min(value, _MAX_TOKENS_CEILING))


def _system_and_question(request: AIRequest) -> tuple[str, str]:
    """System text and the user's question, read from ``messages`` first.

    Same defect that was fixed for the OpenAI-compatible provider: the question
    was taken only from ``prompt``/``input_text``, while the drawing reader asks
    through ``messages``. On this path that meant Claude received the sheet with
    no question attached at all, and the system prompt was dropped outright.
    """
    system_text = ""
    user_parts: list[str] = []
    for message in request.messages:
        if message.role == "system":
            system_text = message.content
        elif message.role in ("user", "assistant"):
            user_parts.append(message.content)
    question = "\n\n".join(user_parts) or request.prompt or request.input_text or ""
    return system_text, question


def _format_payload(request: AIRequest) -> dict[str, Any]:
    """Поля запроса, которые требуют от Claude нужной формы ответа.

    Схема не передавалась ВООБЩЕ: ни параметром, ни инструментом, ни словами.
    Модель получала просьбу «ответь JSON» только в том виде, в каком её написал
    вызывающий, — то есть как пожелание. Ollama в это время клала схему в
    `format` и получала JSON принудительно.

    Три ступени, потому что ни одна не работает везде:
      * `output_config.format` — настоящий структурированный вывод (устаревший
        top-level `output_format` не использовать);
      * принудительный инструмент — на новейших моделях `tool_choice` типа
        `tool`/`any` возвращает 400, поэтому он средняя ступень, не вершина;
      * схема словами — когда не осталось ничего.
    """
    contract = contract_of(request)
    if contract is None or not contract.schema:
        return {}
    if contract.mechanism is FormatMechanism.NATIVE_SCHEMA:
        return {"output_config": {"format": inline_schema_defs(contract.schema)}}
    if contract.mechanism is FormatMechanism.FORCED_TOOL:
        return {
            "tools": [
                {
                    "name": contract.schema_name,
                    "description": "Верни ответ строго по этой схеме.",
                    "input_schema": inline_schema_defs(contract.schema),
                }
            ],
            "tool_choice": {"type": "tool", "name": contract.schema_name},
        }
    return {}


def _answer_from_forced_tool(body: dict, request: AIRequest) -> tuple[str, dict | None]:
    """Ответ, отданный принудительным инструментом, — как текст И как данные.

    Текстовый разбор ниже по потоку рассчитан на строку, поэтому аргументы
    инструмента сериализуются обратно: иначе ответ, полученный самым надёжным
    способом, оказался бы единственным, который никто не читает.
    """
    contract = contract_of(request)
    if contract is None or contract.mechanism is not FormatMechanism.FORCED_TOOL:
        return "", None
    for block in body.get("content") or []:
        if block.get("type") == "tool_use" and block.get("name") == contract.schema_name:
            data = block.get("input")
            if isinstance(data, dict):
                return json.dumps(data, ensure_ascii=False), data
    return "", None


class AnthropicProvider(AIProvider):
    """Anthropic Messages API provider.

    Tool format is converted internally from the OpenAI convention used
    by the rest of the codebase.
    """

    kind = ProviderKind.ANTHROPIC

    @classmethod
    def from_env(cls, prompt_cache: bool = False) -> AnthropicProvider:
        config = ProviderConfig(
            kind=ProviderKind.ANTHROPIC,
            base_url=_ANTHROPIC_API,
            api_key_env="ANTHROPIC_API_KEY",
            timeout_seconds=180.0,
            is_local=False,
        )
        instance = cls(config)
        instance._prompt_cache = prompt_cache
        return instance

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._prompt_cache: bool = False

    def _api_key(self) -> str:
        # Prefer the runtime-resolved key (from the provider_instances DB row),
        # then fall back to the environment variable.
        if self.config.api_key:
            return self.config.api_key
        env = self.config.api_key_env or "ANTHROPIC_API_KEY"
        return os.getenv(env, "")

    def _base(self) -> str:
        base = str(self.config.base_url or _ANTHROPIC_API).rstrip("/")
        # Allow either ".../v1" or bare host in the registry/instance.
        return base if base.endswith("/v1") else f"{base}/v1"

    def _headers(self, stream: bool = False) -> dict[str, str]:
        h = {
            "x-api-key": self._api_key(),
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if stream:
            h["accept"] = "text/event-stream"
        if self._prompt_cache:
            h["anthropic-beta"] = "prompt-caching-2024-07-31"
        return h

    def _build_system(self, system_text: str) -> str | list[dict]:
        if not self._prompt_cache or not system_text:
            return system_text
        return [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

    @staticmethod
    def _openai_tools_to_anthropic(tools: list[Any]) -> list[dict]:
        result = []
        for t in tools:
            fn = t.get("function", {}) if isinstance(t, dict) else {}
            result.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        return result

    @staticmethod
    def _openai_msgs_to_anthropic(
        messages: list[Any],
    ) -> tuple[str, list[dict]]:
        """Convert OpenAI-format message list to (system_text, anthropic_messages)."""
        system_parts: list[str] = []
        result: list[dict] = []
        pending_ids: list[str] = []
        pending_results: list[dict] = []

        def _flush_results() -> None:
            if pending_results:
                result.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()
                pending_ids.clear()

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls") or []

            if role == "system":
                system_parts.append(content)
                continue

            if role in ("user", "assistant") and pending_results:
                _flush_results()

            if role == "user":
                result.append({"role": "user", "content": content})
            elif role == "assistant":
                if tool_calls:
                    blocks: list[dict] = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for i, tc in enumerate(tool_calls):
                        fn = tc.get("function", {})
                        name = fn.get("name", "unknown")
                        args = fn.get("arguments", {})
                        tc_id = tc.get("id") or f"toolu_{name}_{i}"
                        pending_ids.append(tc_id)
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc_id,
                                "name": name,
                                "input": args
                                if isinstance(args, dict)
                                else json.loads(args or "{}"),
                            }
                        )
                    result.append({"role": "assistant", "content": blocks})
                elif content:
                    result.append({"role": "assistant", "content": content})
            elif role == "tool":
                tc_id = (
                    pending_ids.pop(0) if pending_ids else f"toolu_unknown_{len(pending_results)}"
                )
                pending_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc_id,
                        "content": content,
                    }
                )

        _flush_results()
        return "\n\n".join(p for p in system_parts if p), result

    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        started = time.perf_counter()
        system_text = ""
        if request.messages:
            msgs = [m.model_dump() for m in request.messages]
            system_text, anthropic_msgs = self._openai_msgs_to_anthropic(msgs)
        else:
            text = request.prompt or request.input_text or ""
            anthropic_msgs = [{"role": "user", "content": text}]

        payload: dict[str, Any] = {
            "model": model,
            "messages": anthropic_msgs,
            "max_tokens": _resolve_max_tokens(request),
            **_format_payload(request),
        }
        hint = schema_hint_text(contract_of(request))
        if hint and anthropic_msgs:
            # Схема словами дописывается к ПОСЛЕДНЕМУ сообщению пользователя:
            # прикреплённая к первому, она уезжает из внимания модели на длинном
            # диалоге ровно тогда, когда нужна больше всего.
            for message in reversed(anthropic_msgs):
                if message.get("role") == "user" and isinstance(message.get("content"), str):
                    message["content"] = f"{message['content']}{hint}"
                    break
        # Extended thinking: enabled when the caller/catalog asks for CoT.
        # Which shape the model accepts depends on its generation — see
        # thinking_params.anthropic_thinking_payload.
        if request.thinking:
            from app.ai.thinking_params import anthropic_thinking_payload

            thinking_payload = anthropic_thinking_payload(model, request.thinking_level)
            payload.update(thinking_payload)
            budget = (thinking_payload.get("thinking") or {}).get("budget_tokens")
            if budget:
                # The legacy budget is spent out of max_tokens, so the ceiling
                # has to clear it or the answer itself has no room left.
                payload["max_tokens"] = max(payload["max_tokens"], int(budget) + 1024)
        if system_text:
            payload["system"] = self._build_system(system_text)
        if request.tools:
            payload["tools"] = self._openai_tools_to_anthropic(
                [
                    {
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.input_schema,
                        }
                    }
                    for t in request.tools
                ]
            )

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            resp = await client.post(
                f"{self._base()}/messages",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()

        text_out = ""
        tool_calls: list[ProposedToolCall] = []
        for block in body.get("content") or []:
            if block.get("type") == "text":
                text_out += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ProposedToolCall(
                        name=block.get("name", ""),
                        arguments=block.get("input", {}),
                    )
                )

        forced_text, forced_data = _answer_from_forced_tool(body, request)
        if forced_text:
            # Ответ пришёл принудительным инструментом: отдаём его и текстом, и
            # данными — весь разбор ниже по потоку рассчитан на строку. Из
            # `proposed_tool_calls` его надо убрать: там это читалось бы как
            # «модель просит вызвать инструмент», чего она не делала.
            text_out = forced_text
            answer_name = contract_of(request).schema_name
            tool_calls = [call for call in tool_calls if call.name != answer_name]

        usage = body.get("usage", {})
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            text=text_out or None,
            data=forced_data,
            proposed_tool_calls=tool_calls,
            usage=AIUsage(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                total_tokens=(usage.get("input_tokens", 0) + usage.get("output_tokens", 0)) or None,
                latency_ms=int((time.perf_counter() - started) * 1000),
            ),
            raw=body,
        )

    async def vision(self, request: AIRequest, model: str) -> AIResponse:
        started = time.perf_counter()
        content: list[dict] = []
        for img in request.images:
            if img.startswith("data:"):
                media, b64 = img.split(",", 1)
                media_type = media.split(";")[0].split(":")[1]
                content.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    }
                )
            else:
                content.append({"type": "image", "source": {"type": "url", "url": img}})
        system_text, question = _system_and_question(request)
        content.append({"type": "text", "text": question + schema_hint_text(contract_of(request))})

        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": _resolve_max_tokens(request),
            **_format_payload(request),
        }
        if system_text:
            payload["system"] = self._build_system(system_text)
        if request.thinking:
            from app.ai.thinking_params import anthropic_thinking_payload

            thinking_payload = anthropic_thinking_payload(model, request.thinking_level)
            payload.update(thinking_payload)
            budget = (thinking_payload.get("thinking") or {}).get("budget_tokens")
            if budget:
                payload["max_tokens"] = max(payload["max_tokens"], int(budget) + 1024)

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            resp = await client.post(
                f"{self._base()}/messages", headers=self._headers(), json=payload
            )
            resp.raise_for_status()
            body = resp.json()

        text_out = "".join(
            b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"
        )
        tool_text, tool_data = _answer_from_forced_tool(body, request)
        if tool_text:
            text_out = tool_text
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            text=text_out or None,
            data=tool_data,
            usage=AIUsage(latency_ms=int((time.perf_counter() - started) * 1000)),
            raw=body,
        )

    async def embedding(self, request: AIRequest, model: str) -> AIResponse:
        raise NotImplementedError("Anthropic does not provide embedding endpoints")
