"""Context-length and capability metadata for LLM models.

Used by the agent loop to decide when to compress context and to route
images only to vision-capable models.
"""

from __future__ import annotations

# context window sizes in tokens
_CONTEXT_LENGTHS: dict[str, int] = {
    # Ollama / local
    "gemma4:e4b": 128_000,
    "gemma4:12b": 128_000,
    "gemma4:26b": 256_000,
    "gemma4:27b": 256_000,
    "qwen3.5:9b": 128_000,
    "qwen3:8b": 128_000,
    "qwen3:14b": 128_000,
    "qwen3:32b": 128_000,
    "qwen3:30b-a3b": 128_000,
    "qwen3:235b-a22b": 128_000,
    "qwen2.5:7b": 128_000,
    "qwen2.5:14b": 128_000,
    "deepseek-r1:7b": 128_000,
    "deepseek-r1:14b": 128_000,
    "deepseek-r1:32b": 128_000,
    "deepseek-r1:70b": 128_000,
    "llama3.3:70b": 128_000,
    "llama3.1:8b": 128_000,
    "mistral:7b": 32_000,
    "phi4:14b": 128_000,
    # Anthropic Claude
    "claude-opus-4-7": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-haiku-20241022": 200_000,
    "claude-3-opus-20240229": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "o1": 200_000,
    "o3-mini": 200_000,
    # DeepSeek (cloud)
    "deepseek-r1": 128_000,
    "deepseek-v3": 128_000,
    "deepseek-chat": 128_000,
    # Gemini
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.0-flash-lite": 1_048_576,
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
    # Qwen cloud (via OpenRouter)
    "qwen/qwen3-235b-a22b": 40_000,
    "qwen/qwen3-32b": 40_000,
}

# models that support vision (image inputs)
_VISION_MODELS: frozenset[str] = frozenset({
    "gemma4:e4b", "gemma4:12b", "gemma4:26b", "gemma4:27b",
    "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5",
    "claude-opus-4", "claude-sonnet-4",
    "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229",
    "gpt-4o", "gpt-4o-mini",
    "gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash",
})

_DEFAULT_CONTEXT = 32_000


def get_model_context_length(model: str) -> int:
    """Return context window size in tokens for *model*.

    Checks exact match first, then prefix match for versioned names, then
    falls back to ``_DEFAULT_CONTEXT``.
    """
    if model in _CONTEXT_LENGTHS:
        return _CONTEXT_LENGTHS[model]
    # Prefix match handles "openrouter/anthropic/claude-sonnet-4-6" → "claude-sonnet-4-6"
    basename = model.split("/")[-1]
    if basename in _CONTEXT_LENGTHS:
        return _CONTEXT_LENGTHS[basename]
    # Partial prefix scan (e.g. "claude-sonnet" matches "claude-sonnet-4-6")
    model_lower = model.lower()
    for key, length in _CONTEXT_LENGTHS.items():
        if key.lower() in model_lower or model_lower in key.lower():
            return length
    return _DEFAULT_CONTEXT


def is_vision_model(model: str) -> bool:
    """Return True if *model* supports image inputs."""
    if model in _VISION_MODELS:
        return True
    basename = model.split("/")[-1]
    return basename in _VISION_MODELS


def estimate_tokens_rough(messages: list[dict]) -> int:
    """Rough token estimate: total characters of all content divided by 4."""
    total_chars = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_chars += len(str(block.get("text") or block.get("content") or ""))
        for tc in msg.get("tool_calls") or []:
            total_chars += len(str(tc))
    return total_chars // 4
