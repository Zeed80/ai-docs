from __future__ import annotations

import time
from typing import Any

import httpx

from app.ai.providers.base import AIProvider
from app.ai.schemas import AIRequest, AIResponse, AIUsage, ProviderKind


def _inference_options(request: AIRequest, default_temperature: float = 0.2) -> dict[str, Any]:
    """Build Ollama options dict from request inference_params metadata."""
    params = (request.metadata or {}).get("inference_params") or {}
    opts: dict[str, Any] = {"temperature": params.get("temperature", default_temperature)}
    if "top_p" in params:
        opts["top_p"] = params["top_p"]
    if "top_k" in params:
        opts["top_k"] = params["top_k"]
    if "repeat_penalty" in params:
        opts["repeat_penalty"] = params["repeat_penalty"]
    return opts


def _pydantic_to_ollama_format(schema_cls: Any) -> dict[str, Any] | None:
    """Convert a Pydantic model class to an Ollama-compatible JSON schema for structured output."""
    try:
        schema = schema_cls.model_json_schema()
        return {"type": "object", **{k: v for k, v in schema.items() if k != "title"}}
    except Exception:
        return None


def _ollama_keep_alive(model: str) -> str:
    """Return keep_alive duration for a model.

    Strategy:
      - Embedding / reranker models: 10 minutes (called frequently, cheap to keep)
      - Vision / large reasoning models: 5 minutes (valuable VRAM, unload sooner)
      - Default: 5 minutes

    Override via env OLLAMA_KEEP_ALIVE (e.g. "0" to always immediately unload,
    "30m" for 30 minutes). Useful when GPU is shared with vLLM.
    """
    import os
    env_override = os.environ.get("OLLAMA_KEEP_ALIVE", "").strip()
    if env_override:
        return env_override
    model_lower = model.lower()
    if any(x in model_lower for x in ("embed", "rerank", "nomic", "bge")):
        return "10m"
    if any(x in model_lower for x in ("35b", "31b", "30b", "27b", "26b")):
        return "3m"
    return "5m"


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
            "options": _inference_options(request, default_temperature=0.2),
            "keep_alive": _ollama_keep_alive(model),
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
            "options": _inference_options(request, default_temperature=0.0),
            "keep_alive": _ollama_keep_alive(model),
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
        """Vision call via /api/generate with proper system prompt and JSON format.

        Uses /api/generate (not /api/chat) because many Ollama vision models
        (qwen2.5, qwen3.x, llava, gemma4) handle images through the generate
        endpoint. The system field carries the JSON schema instruction and
        format="json" forces structured output.
        """
        started = time.perf_counter()

        # Extract system message from request (e.g. DRAWING_ANALYSIS_SYSTEM_PROMPT)
        system_text = ""
        user_parts: list[str] = []
        for msg in request.messages:
            if msg.role == "system":
                system_text = msg.content
            elif msg.role in ("user", "assistant"):
                user_parts.append(msg.content)

        # Fall back to legacy flat prompt if messages not structured
        if not user_parts:
            prompt_text = request.prompt or request.input_text or ""
        else:
            prompt_text = "\n\n".join(user_parts)

        opts = _inference_options(request, default_temperature=0.0)
        opts["num_predict"] = 8192
        payload: dict = {
            "model": model,
            "prompt": prompt_text,
            "images": [_ollama_image_payload(img) for img in request.images],
            "stream": False,
            "options": opts,
        }
        if system_text:
            payload["system"] = system_text
        # Note: format="json" is intentionally NOT set here — many vision models
        # (qwen3.x, llava) return empty response when format is forced. JSON output
        # is controlled via the system prompt instruction instead.

        # Vision inference needs much more time than text tasks (4-11 min for large models)
        vision_timeout = max(self.config.timeout_seconds, 660.0)
        async with httpx.AsyncClient(timeout=vision_timeout) as client:
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
        payload = {
            "model": model,
            "input": request.input_text or request.prompt or "",
            "keep_alive": _ollama_keep_alive(model),
        }
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
