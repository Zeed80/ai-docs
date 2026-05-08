from __future__ import annotations

import time
from typing import Any

import httpx

from app.ai.providers.base import AIProvider
from app.ai.schemas import AIRequest, AIResponse, AIUsage, ProviderKind


def _pydantic_to_ollama_format(schema_cls: Any) -> dict[str, Any] | None:
    """Convert a Pydantic model class to an Ollama-compatible JSON schema for structured output."""
    try:
        schema = schema_cls.model_json_schema()
        return {"type": "object", **{k: v for k, v in schema.items() if k != "title"}}
    except Exception:
        return None


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

    async def structured_extract(self, request: AIRequest, model: str) -> AIResponse:
        """Use Ollama's structured output (format param) when a schema is provided."""
        if request.response_schema is None:
            return await self.chat(request, model)
        started = time.perf_counter()
        messages = [message.model_dump() for message in request.messages]
        if not messages:
            messages = [{"role": "user", "content": request.prompt or request.input_text or ""}]
        fmt = _pydantic_to_ollama_format(request.response_schema)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        if fmt:
            payload["format"] = fmt
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

    async def rerank(self, request: AIRequest, model: str) -> AIResponse:
        """Rerank via cosine similarity using Ollama's embed API.

        Ollama 0.21.x has no native /api/rerank endpoint. BGE-reranker and
        similar cross-encoder models loaded in Ollama function as embedding
        models. We embed the query and each passage separately and compute
        normalised cosine similarity as the relevance score.
        """
        started = time.perf_counter()
        query = request.input_text or ""
        documents: list[str] = (request.metadata or {}).get("documents", [])
        base_url = str(self.config.base_url).rstrip("/")

        if not documents:
            return AIResponse(
                task=request.task,
                provider=self.kind,
                model=model,
                scores=[],
                usage=AIUsage(latency_ms=0),
            )

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            resp = await client.post(
                f"{base_url}/api/embed",
                json={"model": model, "input": f"query: {query}"},
            )
            resp.raise_for_status()
            q_vec: list[float] = resp.json().get("embeddings", [[]])[0]

            scores: list[float] = []
            for doc in documents:
                resp = await client.post(
                    f"{base_url}/api/embed",
                    json={"model": model, "input": f"passage: {doc[:2000]}"},
                )
                resp.raise_for_status()
                d_vec: list[float] = resp.json().get("embeddings", [[]])[0]
                scores.append(_cosine_to_score(q_vec, d_vec))

        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            scores=scores,
            usage=AIUsage(latency_ms=int((time.perf_counter() - started) * 1000)),
        )


def _cosine_to_score(a: list[float], b: list[float]) -> float:
    """Cosine similarity mapped to [0, 1]. Returns 0.5 on empty vectors."""
    if not a or not b:
        return 0.5
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.5
    return max(0.0, min(1.0, (dot / (norm_a * norm_b) + 1.0) / 2.0))


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
