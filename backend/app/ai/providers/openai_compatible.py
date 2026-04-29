from __future__ import annotations

import os
import time
from typing import Any

import httpx

from app.ai.providers.base import AIProvider
from app.ai.schemas import (
    AIRequest,
    AIResponse,
    AIUsage,
    ChatMessage,
    ProviderKind,
    ProposedToolCall,
)


class OpenAICompatibleProvider(AIProvider):
    kind = ProviderKind.OPENAI_COMPATIBLE

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.config.extra_headers}
        if self.config.api_key_env:
            api_key = os.getenv(self.config.api_key_env)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _messages(self, request: AIRequest) -> list[dict[str, Any]]:
        if request.messages:
            return [message.model_dump() for message in request.messages]
        if request.prompt:
            return [ChatMessage(role="user", content=request.prompt).model_dump()]
        if request.input_text:
            return [ChatMessage(role="user", content=request.input_text).model_dump()]
        return []

    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._messages(request),
            "temperature": 0.2,
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
                f"{str(self.config.base_url).rstrip('/')}/v1/chat/completions",
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
            content.append({"type": "image_url", "image_url": {"url": image}})
        content.append({"type": "text", "text": request.prompt or request.input_text or ""})
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                f"{str(self.config.base_url).rstrip('/')}/v1/chat/completions",
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
                f"{str(self.config.base_url).rstrip('/')}/v1/embeddings",
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
                f"{str(self.config.base_url).rstrip('/')}/v1/rerank",
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
