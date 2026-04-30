from __future__ import annotations

import time
from typing import Any

import httpx

from app.ai.providers.base import AIProvider
from app.ai.schemas import AIRequest, AIResponse, AIUsage, ProviderKind


class OllamaProvider(AIProvider):
    kind = ProviderKind.OLLAMA

    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        started = time.perf_counter()
        messages = [message.model_dump() for message in request.messages]
        if not messages:
            messages = [{"role": "user", "content": request.prompt or request.input_text or ""}]
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.2},
        }
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                f"{str(self.config.base_url).rstrip('/')}/api/chat",
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        text = body.get("message", {}).get("content")
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            text=text,
            usage=AIUsage(
                input_tokens=body.get("prompt_eval_count"),
                output_tokens=body.get("eval_count"),
                total_tokens=_sum_optional(body.get("prompt_eval_count"), body.get("eval_count")),
                latency_ms=int((time.perf_counter() - started) * 1000),
            ),
            raw=body,
        )

    async def vision(self, request: AIRequest, model: str) -> AIResponse:
        started = time.perf_counter()
        payload = {
            "model": model,
            "prompt": _request_prompt(request),
            "images": [_ollama_image_payload(image) for image in request.images],
            "stream": False,
            "options": {"temperature": 0.1},
        }
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                f"{str(self.config.base_url).rstrip('/')}/api/generate",
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            text=body.get("response"),
            usage=AIUsage(
                input_tokens=body.get("prompt_eval_count"),
                output_tokens=body.get("eval_count"),
                total_tokens=_sum_optional(body.get("prompt_eval_count"), body.get("eval_count")),
                latency_ms=int((time.perf_counter() - started) * 1000),
            ),
            raw=body,
        )

    async def embedding(self, request: AIRequest, model: str) -> AIResponse:
        started = time.perf_counter()
        payload = {"model": model, "input": request.input_text or request.prompt or ""}
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                f"{str(self.config.base_url).rstrip('/')}/api/embed",
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        embeddings = body.get("embeddings") or []
        embedding = embeddings[0] if embeddings else []
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            embedding=embedding,
            usage=AIUsage(latency_ms=int((time.perf_counter() - started) * 1000)),
            raw=body,
        )


def _sum_optional(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _request_prompt(request: AIRequest) -> str:
    if request.prompt or request.input_text:
        return request.prompt or request.input_text or ""
    return "\n\n".join(
        message.content
        for message in request.messages
        if message.role in {"system", "user"} and message.content
    )


def _ollama_image_payload(image: str) -> str:
    if image.startswith("data:") and ";base64," in image:
        return image.split(";base64,", 1)[1]
    return image
