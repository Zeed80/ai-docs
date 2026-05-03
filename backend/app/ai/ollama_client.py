"""Ollama client with retry, timeout, circuit breaker.

Dual AI strategy:
- gemma4:e4b (local) — OCR, classification, extraction (confidential documents)
- gemma4:26b (local) or Claude API (remote) — reasoning, letters, NL-query
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger()


class AIBackend(str, Enum):
    OLLAMA = "ollama"
    CLAUDE = "claude"


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Simple circuit breaker for AI backends."""

    failure_threshold: int = 3
    recovery_timeout: float = 60.0
    _failures: int = 0
    _state: CircuitState = CircuitState.CLOSED
    _last_failure_time: float = 0.0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time > self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.time()
        if self._failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning("circuit_breaker_open", failures=self._failures)

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


# Per-model circuit breakers
_breakers: dict[str, CircuitBreaker] = {}


def _get_breaker(model: str) -> CircuitBreaker:
    if model not in _breakers:
        _breakers[model] = CircuitBreaker()
    return _breakers[model]


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
                await _async_sleep(2 ** attempt)

        except httpx.HTTPStatusError as e:
            last_error = e
            breaker.record_failure()
            logger.error("ollama_http_error", model=model, status=e.response.status_code)
            break

    raise RuntimeError(f"Ollama generation failed after {max_retries + 1} attempts: {last_error}")


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
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = text.find(start_char)
        if start != -1:
            depth = 0
            in_str = False
            escape = False
            for i, ch in enumerate(text[start:], start):
                if escape:
                    escape = False
                    continue
                if ch == '\\' and in_str:
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
                        return text[start:i + 1]

    return text.strip()


async def generate_json(
    prompt: str,
    *,
    model: str | None = None,
    system: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    timeout_seconds: float = 120.0,
) -> dict:
    """Generate structured JSON via /api/chat (works for thinking models like Qwen3).

    Uses chat endpoint with think:false to suppress reasoning preamble.
    Falls back to regex JSON extraction if the model wraps output in markdown.
    """
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
        "format": "json",
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }

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
            raw = data.get("message", {}).get("content", "")

            breaker.record_success()
            logger.info(
                "ollama_generate_json",
                model=model,
                elapsed_ms=elapsed_ms,
                text_len=len(raw),
                attempt=attempt + 1,
            )

            cleaned = _extract_json_from_text(raw)
            if not cleaned:
                raise ValueError(f"Model returned empty response (attempt {attempt + 1})")

            return json.loads(cleaned)

        except json.JSONDecodeError as e:
            logger.error("ollama_json_parse_error", model=model, text=raw[:300], error=str(e))
            last_error = ValueError(f"Failed to parse JSON from model output: {e}")
            break  # Don't retry parse errors — bad prompt, not a transient issue

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = e
            breaker.record_failure()
            logger.warning("ollama_json_retry", model=model, attempt=attempt + 1, error=str(e))
            if attempt < 2:
                import asyncio
                await asyncio.sleep(2 ** attempt)

        except Exception as e:
            last_error = e
            breaker.record_failure()
            logger.error("ollama_json_error", model=model, error=str(e))
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

        return OllamaResponse(
            text=data.get("message", {}).get("content", ""),
            model=model,
            total_duration_ms=data.get("total_duration", 0) // 1_000_000,
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
) -> str:
    """Generate using the reasoning backend (local Ollama or Claude API).

    Automatically routes to the configured backend.
    """
    if settings.ai_reasoning_backend == "claude" and settings.anthropic_api_key:
        return await _claude_generate(prompt, system=system, temperature=temperature, max_tokens=max_tokens)

    # Default: local Ollama with reasoning model
    response = await generate(
        prompt,
        model=settings.ollama_model_reasoning,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        format_json=format_json,
    )
    return response.text


async def _claude_generate(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> str:
    """Generate using Claude API (for non-confidential reasoning tasks)."""
    messages = [{"role": "user", "content": prompt}]

    payload: dict = {
        "model": "claude-sonnet-4-6",
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
    import base64

    effective_model = model or getattr(settings, "ollama_model_vlm", settings.ollama_model_ocr)
    breaker = _get_breaker(effective_model)

    if not breaker.is_available:
        raise RuntimeError(f"Circuit breaker open for VLM model {effective_model}")

    b64_images = [base64.b64encode(img).decode("ascii") for img in images]

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": prompt,
        "images": b64_images,
    })

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
            logger.warning("ollama_vlm_retry", model=effective_model, attempt=attempt + 1, error=str(e))
            if attempt < 2:
                await _async_sleep(2 ** attempt)

        except httpx.HTTPStatusError as e:
            last_error = e
            breaker.record_failure()
            logger.error("ollama_vlm_http_error", model=effective_model, status=e.response.status_code)
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
