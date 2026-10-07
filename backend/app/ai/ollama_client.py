"""Ollama client with retry, timeout, circuit breaker.

Dual AI strategy:
- gemma4:e4b (local) — OCR, classification, extraction (confidential documents)
- gemma4:26b (local) or Claude API (remote) — reasoning, letters, NL-query
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
import structlog

from app.config import settings
from app.domain.work_budget_usage import (
    OllamaUsageEvidence,
    ollama_usage_from_body,
    unknown_ollama_usage,
)

logger = structlog.get_logger()


def _ambient_budget_context():
    """Read the server-owned task-local durable context, if one is bound."""
    from app.ai.work_budget_context import current_airouter_budget_context

    return current_airouter_budget_context()


def _capture_budgeted_ollama_response(
    response,
) -> tuple[Any, OllamaUsageEvidence, Exception | None]:
    """Capture status/body usage before client close without hiding its error."""
    try:
        response.raise_for_status()
    except Exception as exc:
        return None, unknown_ollama_usage("http_error"), exc
    try:
        body = response.json()
    except Exception as exc:
        return None, unknown_ollama_usage("response_body_invalid"), exc
    return body, ollama_usage_from_body(body), None


def _strata_structured_output_failed(response) -> str | None:
    """Strata's 502 for a json_object answer that was not valid JSON: its message."""
    if getattr(response, "status_code", None) != 502:
        return None
    try:
        error = (response.json() or {}).get("error") or {}
    except Exception:
        return None
    if "structured_output_failed" not in (error.get("code"), error.get("type")):
        return None
    return str(error.get("message") or "structured_output_failed")[:300]


class AIBackend(str, Enum):
    OLLAMA = "ollama"
    CLAUDE = "claude"


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Simple circuit breaker for AI backends with optional Redis persistence."""

    failure_threshold: int = 3
    recovery_timeout: float = 60.0
    model_name: str = ""
    _failures: int = field(default=0, init=False)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _last_failure_time: float = field(default=0.0, init=False)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time > self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        prev = self._state
        self._state = CircuitState.CLOSED
        if prev != CircuitState.CLOSED and self.model_name:
            _persist_breaker(self.model_name, self)

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.time()
        if self._failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning("circuit_breaker_open", model=self.model_name, failures=self._failures)
            if self.model_name:
                _persist_breaker(self.model_name, self)

    @property
    def is_available(self) -> bool:
        return self.state != CircuitState.OPEN


@dataclass
class OllamaResponse:
    text: str
    model: str
    total_duration_ms: int = 0
    prompt_eval_count: int = 0
    eval_count: int = 0


# Per-model circuit breakers (in-memory, synced to Redis for persistence across restarts)
_breakers: dict[str, CircuitBreaker] = {}
_BREAKER_TTL = 300  # Redis TTL in seconds; auto-clears after 5 min to avoid permanent lockout


def _breaker_redis_key(model: str) -> str:
    return f"circuit_breaker:{model}"


def _load_breaker_from_redis(model: str) -> CircuitBreaker | None:
    """Restore circuit breaker state from Redis if it was OPEN at last shutdown."""
    try:
        from app.utils.redis_client import get_sync_redis

        r = get_sync_redis()
        raw = r.get(_breaker_redis_key(model))
        if not raw:
            return None
        data = json.loads(raw)
        breaker = CircuitBreaker()
        breaker._failures = data.get("failures", 0)
        breaker._last_failure_time = data.get("last_failure_time", 0.0)
        state_str = data.get("state", CircuitState.CLOSED.value)
        breaker._state = CircuitState(state_str)
        return breaker
    except Exception:
        return None


def _persist_breaker(model: str, breaker: CircuitBreaker) -> None:
    """Persist circuit breaker state to Redis so it survives restarts."""
    try:
        from app.utils.redis_client import get_sync_redis

        r = get_sync_redis()
        r.setex(
            _breaker_redis_key(model),
            _BREAKER_TTL,
            json.dumps(
                {
                    "failures": breaker._failures,
                    "state": breaker._state.value,
                    "last_failure_time": breaker._last_failure_time,
                }
            ),
        )
    except Exception:
        pass


def _get_breaker(model: str) -> CircuitBreaker:
    if model not in _breakers:
        restored = _load_breaker_from_redis(model)
        if restored is not None:
            restored.model_name = model
            _breakers[model] = restored
        else:
            _breakers[model] = CircuitBreaker(model_name=model)
    return _breakers[model]


def _runtime_reasoning_model() -> str:
    """Модель рассуждения из маршрутизации задач, с env-запасом.

    Читался ai_config — второе хранилище тех же настроек, куда значение
    попадает только при сохранении из интерфейса. Оно расходилось с
    маршрутизацией месяцами: в докстринге reconcile_ai_config прямо описан
    случай, когда в model_reasoning лежала давно снятая модель.
    """
    try:
        from app.ai.model_resolver import get_reasoning_model

        model = get_reasoning_model().model
        if model and str(model).strip():
            return str(model).strip()
    except Exception:
        pass
    return settings.ollama_model_reasoning


def _runtime_ocr_model_and_provider() -> tuple[str, str]:
    """Модель и провайдер OCR из маршрутизации задач.

    model_resolver сам держит OCR локальным: облачное назначение здесь
    заменяется локальной парой целиком, а не только провайдером.
    """
    try:
        from app.ai.model_resolver import get_ocr_model

        cfg = get_ocr_model()
        if cfg.model and cfg.model.strip():
            return cfg.model.strip(), (cfg.provider or "ollama").strip()
    except Exception:
        pass
    return settings.ollama_model_ocr, "ollama"


def _ensure_gpu_free() -> None:
    """Refuse to trigger an Ollama model load while LoRA training holds the
    GPU. The AI router already gates its own routes, but plenty of legacy
    call sites hit this client directly — one of them loaded a 17GB model
    mid-training and OOM'd the trainer (confirmed live). This is the single
    choke point every Ollama call passes through. Best-effort: a Redis
    hiccup must never break normal inference."""
    try:
        from app.ai import gpu_lock

        if gpu_lock.is_locked():
            raise RuntimeError(gpu_lock.LOCK_MESSAGE)
    except RuntimeError:
        raise
    except Exception:  # noqa: BLE001
        pass


def _ensure_ollama_gpu_allowed() -> None:
    """Refuse a GPU-Ollama call while the card is switched to Strata.

    The router and the agent loop check this themselves; these direct helpers
    did not, and the work-order verifier loaded a 35B model into Ollama beside
    a running Strata (live, 2026-10-06) — Ollama spills it into the RAM that
    Strata's experts occupy.
    """
    from app.ai import gpu_runtime

    gpu_runtime.check_call("ollama", settings.ollama_url)


async def generate(
    prompt: str,
    *,
    model: str | None = None,
    system: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    timeout_seconds: float = 120.0,
    max_retries: int = 2,
    format_json: bool = False,
) -> OllamaResponse:
    """Generate text from Ollama.

    Args:
        prompt: User prompt
        model: Model name (defaults to settings.ollama_model_ocr)
        system: System prompt
        temperature: Sampling temperature
        max_tokens: Max tokens in response
        timeout_seconds: Request timeout
        max_retries: Number of retries
        format_json: Request JSON output format
    """
    budget_context = _ambient_budget_context()
    logical_call_no = None
    if budget_context is not None:
        logical_call_no = await budget_context.begin_direct_text_call(provider="ollama")
    _ensure_gpu_free()
    _ensure_ollama_gpu_allowed()
    model = model or settings.ollama_model_ocr
    breaker = _get_breaker(model)

    if not breaker.is_available:
        raise RuntimeError(f"Circuit breaker open for model {model}")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if format_json:
        payload["format"] = "json"

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            operation_key = None
            dispatched = False
            usage_evidence = unknown_ollama_usage("response_not_observed")
            data = None
            response_error = None
            if budget_context is not None:
                operation_key = await budget_context.prepare_provider_call(
                    logical_call_no=logical_call_no,
                    provider="ollama",
                    provider_attempt=attempt + 1,
                    request={
                        "url": f"{settings.ollama_url}/api/chat",
                        "payload": payload,
                    },
                )
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    start = time.time()
                    dispatched = True
                    response = await client.post(
                        f"{settings.ollama_url}/api/chat",
                        json=payload,
                    )
                    elapsed_ms = int((time.time() - start) * 1000)
                    if budget_context is not None:
                        data, usage_evidence, response_error = _capture_budgeted_ollama_response(
                            response
                        )
            finally:
                if operation_key is not None and dispatched:
                    await budget_context.charge_provider_call(
                        operation_key,
                        usage_evidence=usage_evidence,
                    )
                    await budget_context.assert_direct_text_current()

            if budget_context is not None:
                if response_error is not None:
                    raise response_error
            else:
                response.raise_for_status()
                data = response.json()

            breaker.record_success()

            result = OllamaResponse(
                text=data.get("message", {}).get("content", ""),
                model=model,
                total_duration_ms=data.get("total_duration", 0) // 1_000_000,
                prompt_eval_count=data.get("prompt_eval_count", 0),
                eval_count=data.get("eval_count", 0),
            )

            logger.info(
                "ollama_generate",
                model=model,
                elapsed_ms=elapsed_ms,
                tokens=result.eval_count,
                attempt=attempt + 1,
            )
            return result

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = e
            breaker.record_failure()
            logger.warning(
                "ollama_retry",
                model=model,
                attempt=attempt + 1,
                error=str(e),
            )
            if attempt < max_retries:
                await _async_sleep(2**attempt)

        except httpx.HTTPStatusError as e:
            last_error = e
            breaker.record_failure()
            logger.error("ollama_http_error", model=model, status=e.response.status_code)
            break

    raise RuntimeError(f"Ollama generation failed after {max_retries + 1} attempts: {last_error}")


def salvage_truncated_json(text: str) -> dict | None:
    """Rebuild a usable object from a reply that hit the token ceiling.

    A long extraction ("give me every catalog row") is frequently cut off
    mid-string: the whole reply — hundreds of already-complete rows — is then
    lost to a JSONDecodeError plus retries that truncate at the same place.
    This keeps every element that closed properly and drops the partial tail.

    Returns None when nothing can be recovered, so callers can treat salvage as
    a strict improvement over the exception they would otherwise get.
    """
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    # Offsets where a top-level array element ended cleanly.
    last_complete_element = -1
    array_depth = 0
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            depth += 1
            if ch == "[":
                array_depth += 1
        elif ch in "}]":
            depth -= 1
            if ch == "]":
                array_depth -= 1
            # An element of the outer array closed (array + object still open).
            if ch == "}" and array_depth >= 1 and depth == 2:
                last_complete_element = i
    if last_complete_element == -1:
        return None
    candidate = text[start : last_complete_element + 1] + "]}"
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _extract_json_from_text(text: str) -> str:
    """Strip <think>…</think> blocks and markdown fences, then return the JSON portion."""
    import re

    # Remove Qwen3 / DeepSeek thinking blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if fence:
        text = fence.group(1)

    # Find the outermost JSON object or array
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        if start != -1:
            depth = 0
            in_str = False
            escape = False
            for i, ch in enumerate(text[start:], start):
                if escape:
                    escape = False
                    continue
                if ch == "\\" and in_str:
                    escape = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == start_char:
                    depth += 1
                elif ch == end_char:
                    depth -= 1
                    if depth == 0:
                        return text[start : i + 1]

    return text.strip()


async def _generate_json_anthropic(
    prompt: str,
    *,
    model: str,
    system: str | None,
    temperature: float,
    max_tokens: int,
    timeout_seconds: float,
) -> dict:
    """Generate structured JSON via Anthropic Messages API."""
    messages = [{"role": "user", "content": prompt}]
    payload: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system:
        payload["system"] = system

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    cleaned = _extract_json_from_text(text)
    return json.loads(cleaned or "{}")


async def _generate_json_openai_compatible(
    prompt: str,
    *,
    model: str,
    provider: str,
    system: str | None,
    temperature: float,
    max_tokens: int,
    timeout_seconds: float,
) -> dict:
    """Generate structured JSON via any OpenAI-compatible endpoint (openrouter, deepseek, vllm…)."""
    from app.ai.model_resolver import _provider_api_key, _provider_base_url

    base_url = _provider_base_url(provider)
    api_key = _provider_api_key(provider)
    # Ensure base_url ends with /v1
    if not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # For vLLM: cap max_tokens so that prompt + output fit within max_model_len.
    # vLLM rejects requests where prompt_tokens + max_tokens > max_model_len.
    # Use conservative token estimate (1 token ≈ 3 chars for Cyrillic/mixed content).
    if provider == "vllm":
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        # Conservative: 3 chars/token for Cyrillic-heavy content (vs 4 for ASCII)
        prompt_tokens_est = int(prompt_chars / 3.0)
        try:
            async with httpx.AsyncClient(timeout=5.0) as _c:
                _r = await _c.get(f"{base_url}/models")
                if _r.status_code == 200:
                    _mlen = _r.json()["data"][0].get("max_model_len", 8192)
                    # 512-token safety buffer; at least 512 output tokens
                    max_tokens = min(max_tokens, max(512, _mlen - prompt_tokens_est - 512))
        except Exception:
            max_tokens = min(max_tokens, max(512, 8192 - prompt_tokens_est - 512))

    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    # llamacpp / Qwen3 thinking models: disable CoT so the answer lands in
    # `content` instead of `reasoning_content`. Without this flag the response
    # content is empty and extraction fails.
    if provider == "llamacpp":
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        if provider == "llamacpp":
            raise RuntimeError(
                f"Сервер llamacpp недоступен ({base_url}). "
                "Запустите: docker compose --profile embedded-llamacpp up -d llama-server"
            ) from e
        raise
    resp.raise_for_status()
    raw_content = resp.json()["choices"][0]["message"]["content"]
    if not raw_content:
        # Thinking model may still have put the answer in reasoning_content
        raw_content = resp.json()["choices"][0]["message"].get("reasoning_content", "")
    cleaned = _extract_json_from_text(raw_content)
    return json.loads(cleaned or "{}")


async def generate_json(
    prompt: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    system: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    timeout_seconds: float = 120.0,
    salvage_truncated: bool = False,
    budget_context=None,
) -> dict:
    """Generate structured JSON — routes to the correct backend based on provider.

    ``salvage_truncated``: when the reply is cut off by the token ceiling,
    recover the complete elements instead of raising (opt-in — a caller that
    needs an all-or-nothing object must not silently receive a partial one).

    Provider routing:
      ollama            → Ollama /api/chat (with format:json + think:false)
      llamacpp          → llama-server /v1/chat/completions (OpenAI-compat)
      anthropic         → Anthropic Messages API
      openrouter        → OpenRouter (OpenAI-compat)
      deepseek          → DeepSeek (OpenAI-compat)
      vllm/lmstudio/
      openai_compatible → custom OpenAI-compat URL
      openai/gemini/
      mistral/groq/…    → respective cloud (OpenAI-compat endpoints)

    Falls back to regex JSON extraction if the model wraps output in markdown.
    """
    ambient_budget_context = _ambient_budget_context()
    if budget_context is not None and ambient_budget_context is not None:
        await ambient_budget_context.reject_explicit_budget_context()

    # Existing callers keep the original eager GPU guard.  A budgeted verifier
    # must resolve and validate its provider first so an unsupported route or a
    # zero/legacy budget cannot cause even this provider-side preparation.
    if budget_context is None and ambient_budget_context is None:
        _ensure_gpu_free()
    if model is None or provider is None:
        _model, _provider = _runtime_ocr_model_and_provider()
        model = model or _model
        provider = provider or _provider
    logical_call_no = None
    if budget_context is not None:
        await budget_context.assert_supported_provider(provider)
        await budget_context.preflight_provider_call(provider)
    elif ambient_budget_context is not None:
        logical_call_no = await ambient_budget_context.begin_direct_text_call(provider=provider)
        _ensure_gpu_free()

    breaker = _get_breaker(model)
    if not breaker.is_available:
        raise RuntimeError(f"Circuit breaker open for model {model}")

    # ── Anthropic: own API format ─────────────────────────────────────────────
    if provider == "anthropic":
        try:
            result = await _generate_json_anthropic(
                prompt,
                model=model,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
            )
            breaker.record_success()
            return result
        except Exception as exc:
            breaker.record_failure()
            logger.error("generate_json_anthropic_error", model=model, error=str(exc))
            raise

    # ── OpenAI-compatible cloud providers ─────────────────────────────────────
    _openai_compat_providers = {
        "openrouter",
        "openai",
        "deepseek",
        "gemini",
        "mistral",
        "groq",
        "together",
        "fireworks",
        "xai",
        "cohere",
        "perplexity",
        "minimax",
        "kimi",
        "qwen",
        "vllm",
        "lmstudio",
        "openai_compatible",
    }
    if provider in _openai_compat_providers:
        try:
            result = await _generate_json_openai_compatible(
                prompt,
                model=model,
                provider=provider,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
            )
            breaker.record_success()
            return result
        except Exception as exc:
            breaker.record_failure()
            logger.error(
                "generate_json_cloud_error", provider=provider, model=model, error=str(exc)
            )
            raise

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    use_llamacpp = provider == "llamacpp"
    # Strata goes through this loop, not the cloud helper above: here every
    # physical POST is reserved and charged on a durable budget, like Ollama's.
    use_strata = provider == "strata"
    headers: dict[str, str] = {}

    if use_strata:
        from app.ai.provider_registry import select_instance
        from app.ai.schemas import ProviderKind

        node = select_instance(ProviderKind.STRATA)
        url = f"{node.base_url.rstrip('/').removesuffix('/v1')}/v1/chat/completions"
        if node.api_key:
            headers["Authorization"] = f"Bearer {node.api_key}"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            # Strata's hard off for reasoning: the JSON lands in `content`.
            "reasoning_effort": "none",
        }
    elif use_llamacpp:
        url = f"{settings.llamacpp_url.rstrip('/v1')}/v1/chat/completions"
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            # Qwen3 / thinking models: disable CoT so answer lands in `content` not
            # `reasoning_content`. Without this the model writes <think>…</think> only
            # and content is empty → extract returns "".
            "chat_template_kwargs": {"enable_thinking": False},
        }
    else:
        # Default: Ollama
        _ensure_ollama_gpu_allowed()
        url = f"{settings.ollama_url}/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

    last_error: Exception | None = None
    raw = ""
    for attempt in range(3):
        try:
            operation_key = None
            dispatched = False
            usage_evidence = unknown_ollama_usage("response_not_observed")
            data = None
            response_error = None
            if ambient_budget_context is not None:
                operation_key = await ambient_budget_context.prepare_provider_call(
                    logical_call_no=logical_call_no,
                    provider=provider,
                    provider_attempt=attempt + 1,
                    request={"url": url, "payload": payload},
                )
            try:
                if budget_context is not None:
                    _ensure_gpu_free()
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    if budget_context is not None:
                        operation_key = await budget_context.prepare_provider_call(
                            provider=provider,
                            provider_attempt=attempt + 1,
                            request={"url": url, "payload": payload},
                        )
                    start = time.time()
                    # Treat cancellation/crash after entering this await as a
                    # consumed physical attempt; its committed reservation is
                    # never reopened for automatic replay.
                    dispatched = True
                    response = await client.post(url, json=payload, headers=headers)
                    elapsed_ms = int((time.time() - start) * 1000)
                    if active_budget_context := budget_context or ambient_budget_context:
                        data, usage_evidence, response_error = _capture_budgeted_ollama_response(
                            response
                        )
            finally:
                if operation_key is not None and dispatched:
                    active_budget_context = budget_context or ambient_budget_context
                    await active_budget_context.charge_provider_call(
                        operation_key,
                        usage_evidence=usage_evidence,
                    )
                    if ambient_budget_context is not None:
                        await ambient_budget_context.assert_direct_text_current()

            if use_strata and (strata_error := _strata_structured_output_failed(response)):
                # Strata validates json_object itself and answers 502 when the
                # model's text is not JSON; that is the same retryable case as
                # Ollama returning non-JSON content, not a server failure. The
                # retry drops response_format: Strata's format directive makes
                # the IQ2 model write one-line JSON that broke near char 1000
                # in 8 of 8 verifier calls, while the same request without it
                # was valid 8 of 8 (live 2026-10-07). Our parser checks it.
                raw = ""
                payload = {k: v for k, v in payload.items() if k != "response_format"}
                raise json.JSONDecodeError(
                    f"Strata structured_output_failed: {strata_error}", "", 0
                )
            if budget_context is not None or ambient_budget_context is not None:
                if response_error is not None:
                    raise response_error
            else:
                response.raise_for_status()
                data = response.json()

            if use_llamacpp or use_strata:
                raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            else:
                raw = data.get("message", {}).get("content", "")

            breaker.record_success()
            logger.info(
                "generate_json",
                model=model,
                provider=provider,
                elapsed_ms=elapsed_ms,
                text_len=len(raw),
                attempt=attempt + 1,
            )

            cleaned = _extract_json_from_text(raw)
            if not cleaned:
                raise ValueError(f"Model returned empty response (attempt {attempt + 1})")

            return json.loads(cleaned)

        except json.JSONDecodeError as e:
            if salvage_truncated:
                recovered = salvage_truncated_json(raw)
                if recovered is not None:
                    logger.warning(
                        "generate_json_salvaged_truncated",
                        model=model,
                        error=str(e),
                        attempt=attempt + 1,
                    )
                    breaker.record_success()
                    return recovered
            logger.warning(
                "generate_json_parse_error",
                model=model,
                text=raw[:300],
                error=str(e),
                attempt=attempt + 1,
            )
            last_error = ValueError(f"Failed to parse JSON from model output: {e}")
            if attempt < 2:
                import asyncio as _asyncio

                await _asyncio.sleep(2**attempt)

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            breaker.record_failure()
            if provider == "llamacpp":
                last_error = RuntimeError(
                    f"Сервер llamacpp недоступен ({url}). "
                    "Запустите: docker compose --profile embedded-llamacpp up -d llama-server"
                )
                logger.error("llamacpp_unreachable", url=url, error=str(e))
                break  # no point retrying — server is simply not running
            last_error = e
            logger.warning(
                "generate_json_retry",
                model=model,
                provider=provider,
                attempt=attempt + 1,
                error=str(e),
            )
            if attempt < 2:
                import asyncio

                await asyncio.sleep(2**attempt)

        except Exception as e:
            last_error = e
            breaker.record_failure()
            logger.error("generate_json_error", model=model, provider=provider, error=str(e))
            break

    raise last_error or RuntimeError("generate_json failed")


async def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout_seconds: float = 120.0,
    format_json: bool = False,
) -> OllamaResponse:
    """Chat-style generation using Ollama /api/chat."""
    budget_context = _ambient_budget_context()
    logical_call_no = None
    if budget_context is not None:
        logical_call_no = await budget_context.begin_direct_text_call(provider="ollama")
    _ensure_gpu_free()
    _ensure_ollama_gpu_allowed()
    model = model or settings.ollama_model_reasoning
    breaker = _get_breaker(model)

    if not breaker.is_available:
        raise RuntimeError(f"Circuit breaker open for model {model}")

    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if format_json:
        payload["format"] = "json"

    try:
        operation_key = None
        dispatched = False
        usage_evidence = unknown_ollama_usage("response_not_observed")
        data = None
        response_error = None
        if budget_context is not None:
            operation_key = await budget_context.prepare_provider_call(
                logical_call_no=logical_call_no,
                provider="ollama",
                provider_attempt=1,
                request={"url": f"{settings.ollama_url}/api/chat", "payload": payload},
            )
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                start = time.time()
                dispatched = True
                response = await client.post(
                    f"{settings.ollama_url}/api/chat",
                    json=payload,
                )
                elapsed_ms = int((time.time() - start) * 1000)
                if budget_context is not None:
                    data, usage_evidence, response_error = _capture_budgeted_ollama_response(
                        response
                    )
        finally:
            if operation_key is not None and dispatched:
                await budget_context.charge_provider_call(
                    operation_key,
                    usage_evidence=usage_evidence,
                )
                await budget_context.assert_direct_text_current()

        if budget_context is not None:
            if response_error is not None:
                raise response_error
        else:
            response.raise_for_status()
            data = response.json()
        breaker.record_success()

        return OllamaResponse(
            text=data.get("message", {}).get("content", ""),
            model=model,
            # total_duration — время самой генерации по версии сервера; оно не
            # включает ожидание в очереди и сеть, а при разгрузке модели
            # приходит нулём. Замеренное здесь elapsed_ms — то, что реально
            # ждал вызывающий; раньше оно вычислялось и выбрасывалось.
            total_duration_ms=(data.get("total_duration", 0) // 1_000_000) or elapsed_ms,
            eval_count=data.get("eval_count", 0),
        )

    except Exception as e:
        breaker.record_failure()
        raise RuntimeError(f"Ollama chat failed: {e}")


async def reasoning_generate(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    format_json: bool = False,
    confidential: bool = False,
) -> str:
    """Generate using the reasoning backend.

    The model and provider come from task routing via ``get_reasoning_model``
    — the same assignment the Models screen shows. Previously they were read
    from ``ai_config``, a second store of the same setting that only received
    a value when someone saved the GUI form, so it drifted away from the
    assignment and stayed wrong until the next save.

    When confidential=True, only local providers (ollama/llamacpp) are used.
    """
    from app.ai.model_resolver import get_reasoning_model

    cfg = get_reasoning_model(confidential=confidential)
    model_name = cfg.model
    provider = cfg.provider
    budget_context = _ambient_budget_context()
    if budget_context is not None:
        await budget_context.preflight_direct_text_call(provider)

    # ── Cloud providers ───────────────────────────────────────────────────────
    if not confidential and provider == "anthropic" and settings.anthropic_api_key:
        return await _claude_generate(
            prompt,
            model=model_name,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    cloud_compat = provider in (
        "openrouter",
        "openai",
        "deepseek",
        "gemini",
        "mistral",
        "groq",
        "together",
        "fireworks",
        "xai",
        "cohere",
        "perplexity",
        "minimax",
        "kimi",
        "qwen",
    )
    if provider == "strata":
        # Local, so confidential content may go there. One POST, reserved and
        # charged on a durable budget when there is one (usage: explicit unknown).
        return await _strata_reasoning_text(
            prompt,
            model=model_name,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            format_json=format_json,
            budget_context=budget_context,
        )

    if cloud_compat and not confidential:
        from app.ai.model_resolver import _provider_api_key, _provider_base_url

        base_url = _provider_base_url(provider)
        if not base_url.endswith("/v1"):
            base_url = base_url.rstrip("/") + "/v1"
        api_key = _provider_api_key(provider)
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if format_json:
            payload["response_format"] = {"type": "json_object"}
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=float(max_tokens * 0.1 + 60)) as client:
            resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    # ── llamacpp ─────────────────────────────────────────────────────────────
    if provider == "llamacpp":
        llamacpp_url = f"{settings.llamacpp_url.rstrip('/v1')}/v1/chat/completions"
        messages_lc: list[dict] = []
        if system:
            messages_lc.append({"role": "system", "content": system})
        messages_lc.append({"role": "user", "content": prompt})
        payload_lc: dict = {
            "model": model_name,
            "messages": messages_lc,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if format_json:
            payload_lc["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=float(max_tokens * 0.1 + 60)) as client:
            resp_lc = await client.post(llamacpp_url, json=payload_lc)
        resp_lc.raise_for_status()
        return resp_lc.json()["choices"][0]["message"]["content"]

    # ── Legacy fallback: check old ai_reasoning_backend setting ───────────────
    if (
        budget_context is None
        and not confidential
        and settings.ai_reasoning_backend == "claude"
        and settings.anthropic_api_key
    ):
        return await _claude_generate(
            prompt,
            model=model_name,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    # Default: local Ollama with reasoning model
    response = await generate(
        prompt,
        model=model_name,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        format_json=format_json,
    )
    return response.text


async def _strata_reasoning_text(
    prompt: str,
    *,
    model: str,
    system: str | None,
    temperature: float,
    max_tokens: int,
    format_json: bool,
    budget_context: Any | None,
) -> str:
    from app.ai.provider_registry import select_instance
    from app.ai.schemas import ProviderKind

    node = select_instance(ProviderKind.STRATA)
    url = f"{node.base_url.rstrip('/').removesuffix('/v1')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {node.api_key}"} if node.api_key else {}
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "reasoning_effort": "none",
    }
    if format_json:
        payload["response_format"] = {"type": "json_object"}
    operation_key = None
    if budget_context is not None:
        logical_call_no = await budget_context.begin_direct_text_call(provider="strata")
        operation_key = await budget_context.prepare_provider_call(
            logical_call_no=logical_call_no,
            provider="strata",
            provider_attempt=1,
            request={"url": url, "payload": payload},
        )
    usage_evidence = unknown_ollama_usage("response_not_observed")
    data = None
    response_error: Exception | None = None
    try:
        async with httpx.AsyncClient(timeout=float(max_tokens * 0.1 + 120)) as client:
            response = await client.post(url, headers=headers, json=payload)
            data, usage_evidence, response_error = _capture_budgeted_ollama_response(response)
    finally:
        if operation_key is not None:
            await budget_context.charge_provider_call(operation_key, usage_evidence=usage_evidence)
            await budget_context.assert_direct_text_current()
    if response_error is not None:
        raise response_error
    return (data or {}).get("choices", [{}])[0].get("message", {}).get("content") or ""


async def _claude_generate(
    prompt: str,
    *,
    model: str = "claude-sonnet-4-6",
    system: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> str:
    """Generate using Claude API (for non-confidential reasoning tasks)."""
    messages = [{"role": "user", "content": prompt}]

    payload: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system:
        payload["system"] = system

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        )

    response.raise_for_status()
    data = response.json()
    return data["content"][0]["text"]


async def chat_with_images(
    prompt: str,
    images: list[bytes],
    *,
    model: str | None = None,
    system: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 8192,
    timeout_seconds: float = 180.0,
    format_json: bool = False,
) -> OllamaResponse:
    """Send a chat request to a VLM (Vision Language Model) with image attachments.

    Images are passed as base64-encoded bytes in the Ollama /api/chat `images` field.
    Supports models: gemma4, llava, llava-llama3, minicpm-v, qwen2-vl, etc.

    Args:
        prompt: Text prompt
        images: List of raw image bytes (PNG, JPEG, etc.)
        model: Ollama model name (defaults to settings.ollama_model_vlm)
        system: System prompt
        temperature: Sampling temperature
        max_tokens: Max response tokens
        timeout_seconds: Request timeout (VLM inference is slow)
        format_json: Request JSON structured output
    """
    _ensure_gpu_free()
    _ensure_ollama_gpu_allowed()
    import base64

    effective_model = model or getattr(settings, "ollama_model_vlm", settings.ollama_model_ocr)
    breaker = _get_breaker(effective_model)

    if not breaker.is_available:
        raise RuntimeError(f"Circuit breaker open for VLM model {effective_model}")

    b64_images = [base64.b64encode(img).decode("ascii") for img in images]

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append(
        {
            "role": "user",
            "content": prompt,
            "images": b64_images,
        }
    )

    payload: dict = {
        "model": effective_model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if format_json:
        payload["format"] = "json"

    last_error: Exception | None = None

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                start = time.time()
                response = await client.post(
                    f"{settings.ollama_url}/api/chat",
                    json=payload,
                )
                elapsed_ms = int((time.time() - start) * 1000)

            response.raise_for_status()
            data = response.json()
            breaker.record_success()

            result = OllamaResponse(
                text=data.get("message", {}).get("content", ""),
                model=effective_model,
                total_duration_ms=data.get("total_duration", 0) // 1_000_000,
                prompt_eval_count=data.get("prompt_eval_count", 0),
                eval_count=data.get("eval_count", 0),
            )

            logger.info(
                "ollama_vlm",
                model=effective_model,
                elapsed_ms=elapsed_ms,
                images=len(images),
                tokens=result.eval_count,
                attempt=attempt + 1,
            )
            return result

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = e
            breaker.record_failure()
            logger.warning(
                "ollama_vlm_retry", model=effective_model, attempt=attempt + 1, error=str(e)
            )
            if attempt < 2:
                await _async_sleep(2**attempt)

        except httpx.HTTPStatusError as e:
            last_error = e
            breaker.record_failure()
            logger.error(
                "ollama_vlm_http_error", model=effective_model, status=e.response.status_code
            )
            break

        except Exception as e:
            last_error = e
            breaker.record_failure()
            logger.error("ollama_vlm_error", model=effective_model, error=str(e))
            break

    raise RuntimeError(f"VLM chat failed after attempts: {last_error}")


async def check_health() -> dict:
    """Check Ollama health and list available models."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.ollama_url}/api/tags")
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        return {"status": "ok", "models": models}
    except Exception as e:
        return {"status": "unavailable", "error": str(e)}


async def _async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
