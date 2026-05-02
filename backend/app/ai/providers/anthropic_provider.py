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

from app.ai.providers.base import AIProvider
from app.ai.schemas import (
    AIRequest,
    AIResponse,
    AIUsage,
    ProviderConfig,
    ProviderKind,
    ProposedToolCall,
)

_ANTHROPIC_API = "https://api.anthropic.com/v1"
_ANTHROPIC_VERSION = "2023-06-01"
_MAX_TOKENS = 4096


class AnthropicProvider(AIProvider):
    """Anthropic Messages API provider.

    Tool format is converted internally from the OpenAI convention used
    by the rest of the codebase.
    """

    kind = ProviderKind.ANTHROPIC

    @classmethod
    def from_env(cls, prompt_cache: bool = False) -> "AnthropicProvider":
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
        env = self.config.api_key_env or "ANTHROPIC_API_KEY"
        return os.getenv(env, "")

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
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
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
                        blocks.append({
                            "type": "tool_use",
                            "id": tc_id,
                            "name": name,
                            "input": args if isinstance(args, dict) else json.loads(args or "{}"),
                        })
                    result.append({"role": "assistant", "content": blocks})
                elif content:
                    result.append({"role": "assistant", "content": content})
            elif role == "tool":
                tc_id = pending_ids.pop(0) if pending_ids else f"toolu_unknown_{len(pending_results)}"
                pending_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc_id,
                    "content": content,
                })

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
            "max_tokens": _MAX_TOKENS,
        }
        if system_text:
            payload["system"] = self._build_system(system_text)
        if request.tools:
            payload["tools"] = self._openai_tools_to_anthropic(
                [{"function": {"name": t.name, "description": t.description, "parameters": t.input_schema}} for t in request.tools]
            )

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            resp = await client.post(
                f"{_ANTHROPIC_API}/messages",
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
                tool_calls.append(ProposedToolCall(
                    name=block.get("name", ""),
                    arguments=block.get("input", {}),
                ))

        usage = body.get("usage", {})
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            text=text_out or None,
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
                content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}})
            else:
                content.append({"type": "image", "source": {"type": "url", "url": img}})
        content.append({"type": "text", "text": request.prompt or request.input_text or ""})

        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": _MAX_TOKENS,
        }

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            resp = await client.post(f"{_ANTHROPIC_API}/messages", headers=self._headers(), json=payload)
            resp.raise_for_status()
            body = resp.json()

        text_out = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            text=text_out or None,
            usage=AIUsage(latency_ms=int((time.perf_counter() - started) * 1000)),
            raw=body,
        )

    async def embedding(self, request: AIRequest, model: str) -> AIResponse:
        raise NotImplementedError("Anthropic does not provide embedding endpoints")
