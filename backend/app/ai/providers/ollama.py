from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog

from app.ai.providers.base import AIProvider
from app.ai.schemas import AIRequest, AIResponse, AIUsage, ProviderKind

logger = structlog.get_logger(__name__)


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
    if "num_ctx" in params:
        # Per-task context prevents a short CAD JSON question from allocating
        # the service-wide KV cache. Keep a safe lower bound for images and an
        # upper bound matching the production service configuration. Raised
        # 32768→65536 2026-08-15 for qwen3.8:27b's hybrid attention/SSM
        # architecture: its KV-cache-equivalent state grows far more gently
        # with context than a plain dense-attention model of the same size
        # (measured ~100MB extra VRAM going 8192→32768 on this GPU), so a
        # bigger context here is comparatively cheap for models built that
        # way — still capped, not unlimited.
        opts["num_ctx"] = max(4096, min(int(params["num_ctx"]), 65536))
    return opts


def _think_flag(request: AIRequest) -> bool:
    """Effective thinking/CoT flag for Ollama (default off when unset)."""
    return bool(request.thinking)


def _thinking_payload(request: AIRequest) -> dict[str, Any]:
    """Ollama + model-template controls for the requested reasoning mode.

    ``think=false`` is Ollama's public switch, while several Qwen templates
    also inspect ``enable_thinking``.  Sending both is intentional: the live
    Apex vision model ignored the first switch alone and put its complete JSON
    in the thinking field, leaving an empty answer.
    """

    enabled = _think_flag(request)
    payload: dict[str, Any] = {"think": enabled}
    if not enabled:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def _pydantic_to_ollama_format(schema_cls: Any) -> dict[str, Any] | None:
    """Convert a Pydantic model class to an Ollama-compatible JSON schema for structured output."""
    try:
        schema = schema_cls.model_json_schema()
        return {"type": "object", **{k: v for k, v in schema.items() if k != "title"}}
    except Exception:
        return None


def _recovered_text(
    text: str | None, thinking: str | None, *, model: str, request: AIRequest,
) -> str | None:
    """Recover an answer a thinking-capable model wrote into "thinking"
    instead of the field the caller actually reads.

    Live-reproduced 2026-08-14 on a plain shaft sheet: qwen3-vl:32b given a
    json_schema format put its ENTIRE valid, schema-conforming answer into
    "thinking" and left the answer field empty, on every fragment question,
    with think=false AND chat_template_kwargs.enable_thinking=false both
    already set — the two documented switches in _thinking_payload do not
    stop this model from doing it. The result was 100% of a real CAD
    digitization's reads (part kind, title block, geometry, PMI — every
    fragment across all three consensus passes) silently discarded as "the
    model said nothing", while the correct JSON sat unread the whole time.
    Downstream JSON parsing already tolerates noisy/wrapped text (markdown
    fences etc.), so handing it the thinking text costs nothing on the
    passes where thinking is genuinely just reasoning with no embedded
    answer — parsing simply fails there exactly as it does today.
    """
    if (text or "").strip():
        return text
    thinking = (thinking or "").strip()
    if thinking:
        logger.warning(
            "ollama_empty_response_recovered_from_thinking",
            model=model,
            task=getattr(request.task, "value", str(request.task)),
            thinking_chars=len(thinking),
        )
        return thinking
    return text


def _ollama_keep_alive(model: str) -> str | int:
    """Return keep_alive for a model: -1 for the pinned orchestrator, short
    (ephemeral) otherwise. See app.ai.model_lifecycle for the policy."""
    from app.ai.model_lifecycle import keep_alive_for
    return keep_alive_for(model)


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
            **_thinking_payload(request),
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
        message = body.get("message") or {}
        text = _recovered_text(
            message.get("content"), message.get("thinking"),
            model=model, request=request,
        )
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
            **_thinking_payload(request),
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
        message = body.get("message") or {}
        text = _recovered_text(
            message.get("content"), message.get("thinking"),
            model=model, request=request,
        )
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
        requested_num_predict = (request.metadata or {}).get("num_predict")
        # Ceiling raised from 8192 on 2026-07-25: a full EngineeringDrawingSpec
        # for a real A3 sheet does not fit — Ollama escapes Cyrillic as \uXXXX,
        # so the JSON ran out of output room and came back truncated mid-object,
        # which the caller could only report as "unreadable drawing". The
        # DEFAULT is unchanged; only an explicit request can go higher.
        opts["num_predict"] = (
            max(1, min(int(requested_num_predict), 32768))
            if requested_num_predict is not None
            else 8192
        )
        payload: dict = {
            "model": model,
            "prompt": prompt_text,
            "images": [_ollama_image_payload(img) for img in request.images],
            "stream": False,
            **_thinking_payload(request),
            "options": opts,
        }
        if system_text:
            payload["system"] = system_text
        # Structured output is OPT-IN per call via metadata["json_schema"].
        #
        # An older comment here claimed format=json makes vision models answer
        # with an empty string. Measured against the current reader on a real
        # flange sheet (2026-07-25) that is no longer true, and the difference
        # is large: free-form took 7.7 s and came back wrapped in markdown
        # fences, a schema took 0.3 s, parsed without repair, and filled a
        # thickness the free-form answer had left null. It stays opt-in because
        # the claim may still hold for some model in the catalogue — a caller
        # that asks for a schema has measured its own model.
        json_schema = (request.metadata or {}).get("json_schema")
        if json_schema:
            payload["format"] = json_schema

        # Vision inference needs much more time than text tasks. Raised from
        # 660 s on 2026-07-25: reading a full A3 sheet with qwen3-vl:32b timed
        # out at exactly 660 s, and httpx.ReadTimeout stringifies to "", so the
        # router logged an EMPTY error and silently fell back to the next
        # candidate — the failure looked like a broken model, not a deadline.
        vision_timeout = max(self.config.timeout_seconds, 1800.0)
        async with httpx.AsyncClient(timeout=vision_timeout) as client:
            response = await client.post(
                f"{str(self.config.base_url).rstrip('/')}/api/generate",
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        text = body.get("response")
        if not (text or "").strip():
            # An empty answer has two very different causes and they used to be
            # indistinguishable from the outside. Verified 2026-07-25 on a real
            # sheet: with think=true a model puts everything in "thinking" and
            # leaves "response" empty, and reading only "response" then looks
            # exactly like "the model failed". Say which it was.
            thinking = (body.get("thinking") or "").strip()
            # RECOVER it, don't just diagnose it. Live-reproduced 2026-08-14 on
            # a plain shaft sheet: qwen3-vl:32b + a json_schema format sends the
            # model's ENTIRE valid, schema-conforming answer into "thinking" and
            # leaves "response" empty, on every single fragment question, with
            # think=false AND chat_template_kwargs.enable_thinking=false both
            # set — the two documented switches from _thinking_payload do not
            # stop this model from doing it. The result was 100% of a real CAD
            # digitization's fragment reads (kind/stamp/callouts/PMI, all three
            # consensus passes) silently discarded as "the model said nothing",
            # while the actual correct JSON sat unread in the response body the
            # whole time. _parse_spec_json downstream already tolerates noisy/
            # wrapped text (markdown fences etc.), so handing it the thinking
            # text costs nothing when it doesn't contain usable JSON, either.
            if thinking:
                text = thinking
            logger.warning(
                "ollama_vision_empty_response",
                model=model,
                task=getattr(request.task, "value", str(request.task)),
                images=len(request.images or []),
                thinking_chars=len(thinking),
                reason=(
                    "answer_went_to_thinking_field" if thinking
                    else "model_returned_nothing"
                ),
                recovered_from_thinking=bool(thinking),
                eval_count=body.get("eval_count"),
                prompt_eval_count=body.get("prompt_eval_count"),
            )
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
        """Cross-encoder reranking.

        When the model advertises the ``completion`` capability (a generate-capable
        reranker), each (query, document) pair is scored *jointly* via
        ``/api/generate`` using the yes/no template, reading the log-probabilities
        of the first generated token: ``score = P("yes")`` — a true cross-encoder
        judgement.

        Otherwise we fall back to bi-encoder cosine over separately-embedded
        query/passage. Note: some reranker GGUFs (e.g. Qwen3-Reranker on Ollama
        0.30.x) expose ``embedding`` but return zero vectors — this is detected
        and surfaced as *empty scores* so the caller skips reranking entirely
        rather than corrupting the ranking with constant 0.5 scores.
        """
        started = time.perf_counter()
        query = request.input_text or ""
        documents: list[str] = (request.metadata or {}).get("documents", [])
        base_url = str(self.config.base_url).rstrip("/")

        if not documents:
            return AIResponse(
                task=request.task, provider=self.kind, model=model,
                scores=[], usage=AIUsage(latency_ms=0),
            )

        scores: list[float] = []
        if await self._supports_generate(base_url, model):
            try:
                scores = await self._rerank_via_generate(base_url, model, query, documents)
            except Exception:
                scores = []
        if not scores:
            scores = await self._rerank_via_embed(base_url, model, query, documents)

        return AIResponse(
            task=request.task, provider=self.kind, model=model, scores=scores,
            usage=AIUsage(latency_ms=int((time.perf_counter() - started) * 1000)),
        )

    async def _supports_generate(self, base_url: str, model: str) -> bool:
        """Whether the model can serve /api/generate (cached per process)."""
        cache_key = (base_url, model)
        cached = _GENERATE_CAP_CACHE.get(cache_key)
        if cached is not None:
            return cached
        supported = False
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                resp = await client.post(f"{base_url}/api/show", json={"model": model})
                resp.raise_for_status()
                caps = resp.json().get("capabilities") or []
                supported = "completion" in caps
        except Exception:
            supported = False
        _GENERATE_CAP_CACHE[cache_key] = supported
        return supported

    async def _rerank_via_generate(
        self, base_url: str, model: str, query: str, documents: list[str]
    ) -> list[float]:
        """LLM-as-reranker: a generate-capable model judges each (query, document)
        pair jointly and we read the yes/no log-probabilities of the first token
        (``score = P("yes")``) — a true cross-encoder relevance signal.

        Model-agnostic (uses the model's own chat template, ``raw=False``) so any
        completion model works; thinking is disabled so the first token is the
        verdict. Candidates are scored with bounded concurrency to keep latency
        low (Ollama pipelines small models well)."""
        sem = asyncio.Semaphore(_RERANK_CONCURRENCY)

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            async def score_one(doc: str) -> float:
                prompt = (
                    "Ты — судья релевантности поиска. Оцени, помогает ли документ "
                    "ответить на запрос.\n"
                    f"Запрос: \"{query}\"\n"
                    f"Документ: \"{doc[:4000]}\"\n"
                    "Ответь строго одним словом: yes или no."
                )
                async with sem:
                    resp = await client.post(
                        f"{base_url}/api/generate",
                        json={
                            "model": model,
                            "prompt": prompt,
                            "stream": False,
                            "think": False,
                            "options": {"temperature": 0.0, "num_predict": 1},
                            "logprobs": True,
                            "top_logprobs": 20,
                            "keep_alive": _ollama_keep_alive(model),
                        },
                    )
                    resp.raise_for_status()
                    return _rerank_score_from_response(resp.json())

            return list(await asyncio.gather(*[score_one(d) for d in documents]))

    async def _rerank_via_embed(
        self, base_url: str, model: str, query: str, documents: list[str]
    ) -> list[float]:
        """Bi-encoder fallback: cosine of separately-embedded query/passage.

        Returns [] (→ caller skips reranking) when the model yields a zero query
        vector, which signals a reranker GGUF that does not actually produce
        embeddings — scoring it would assign a constant 0.5 to every document.
        """
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            resp = await client.post(
                f"{base_url}/api/embed",
                json={"model": model, "input": f"query: {query}"},
            )
            resp.raise_for_status()
            q_vec: list[float] = resp.json().get("embeddings", [[]])[0]
            if not _has_signal(q_vec):
                return []

            scores: list[float] = []
            for doc in documents:
                resp = await client.post(
                    f"{base_url}/api/embed",
                    json={"model": model, "input": f"passage: {doc[:2000]}"},
                )
                resp.raise_for_status()
                d_vec: list[float] = resp.json().get("embeddings", [[]])[0]
                scores.append(_cosine_to_score(q_vec, d_vec))
        return scores


# Per-process cache of (base_url, model) → supports /api/generate. Avoids an
# /api/show round-trip (and a guaranteed 400) on every rerank for embed-only
# reranker models.
_GENERATE_CAP_CACHE: dict[tuple[str, str], bool] = {}


def _has_signal(vec: list[float]) -> bool:
    """True if the vector is non-empty and not all-zeros."""
    return bool(vec) and any(x != 0.0 for x in vec)


# Bounded concurrency for LLM-as-reranker scoring of candidates.
_RERANK_CONCURRENCY = 8


def _rerank_score_from_response(body: dict[str, Any]) -> float:
    """Relevance in [0, 1] from an LLM-as-reranker generate response.

    Preferred path: softmax over the yes/no log-probabilities of the first
    generated token. Fallback: parse the generated text (yes/да→1.0 / no/нет→0.0).
    """
    score = _score_from_logprobs(body.get("logprobs"))
    if score is not None:
        return score
    text = (body.get("response") or "").strip().lower()
    if text.startswith(("yes", "да")):
        return 1.0
    if text.startswith(("no", "нет")):
        return 0.0
    return 0.5


def _score_from_logprobs(logprobs: Any) -> float | None:
    """P("yes") from the first token's candidate log-probabilities.

    Defensive against the exact JSON shape: accepts top-level chosen-token
    entries with a nested ``top_logprobs`` list, snake/camel key variants, and
    leading-space token text. Returns None when no yes/no candidate is present
    so the caller can fall back to text parsing.
    """
    if not isinstance(logprobs, list) or not logprobs:
        return None
    first = logprobs[0]
    if not isinstance(first, dict):
        return None
    entries: list[dict[str, Any]] = []
    candidates = first.get("top_logprobs") or first.get("topLogprobs")
    if isinstance(candidates, list):
        entries.extend(e for e in candidates if isinstance(e, dict))
    if "token" in first:
        entries.append(first)

    yes_lp: float | None = None
    no_lp: float | None = None
    for entry in entries:
        token = str(entry.get("token", "")).strip().lower()
        raw_lp = entry.get("logprob", entry.get("logProb"))
        if raw_lp is None:
            continue
        lp = float(raw_lp)
        if token in {"yes", "y", "да"} and (yes_lp is None or lp > yes_lp):
            yes_lp = lp
        elif token in {"no", "n", "нет"} and (no_lp is None or lp > no_lp):
            no_lp = lp

    if yes_lp is None and no_lp is None:
        return None
    import math

    if no_lp is None:
        return max(0.0, min(1.0, math.exp(yes_lp)))
    if yes_lp is None:
        return max(0.0, min(1.0, 1.0 - math.exp(no_lp)))
    pivot = max(yes_lp, no_lp)
    ey = math.exp(yes_lp - pivot)
    en = math.exp(no_lp - pivot)
    return ey / (ey + en)


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
