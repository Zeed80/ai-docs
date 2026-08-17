"""Single source of truth for translating (thinking, thinking_level) into the
HTTP parameters each provider family expects.

Every provider speaks a different dialect for chain-of-thought control:
  - Ollama: a ``think`` field — bool, or (for models that support it) a
    qualitative level string sent directly.
  - OpenAI-compatible reasoning-effort family (OpenAI o-series, gpt-oss on
    Ollama/vLLM, Groq, xAI, DashScope/Qwen, Cerebras, Ollama Cloud):
    ``reasoning_effort: "none"|"low"|"medium"|"high"``.
  - OpenRouter: ``reasoning: {enabled, effort}``.
  - llama.cpp / vLLM serving a Qwen3-family chat template: only a binary
    ``chat_template_kwargs.enable_thinking`` switch — no granular level.
  - Anthropic: ``thinking: {type, budget_tokens}`` — level maps to a token
    budget, not a string.

``thinking=False`` always yields the provider's hard-off params; ``level`` is
ignored in that case. Callers should already have clamped ``level`` to the
model's declared ``ModelCapability.thinking_levels`` (see
``AIRouter.run``/``agent_loop._thinking_level``) — this module does not know
about the model catalog, only about provider wire formats.
"""

from __future__ import annotations

from typing import Any, Literal

ThinkingLevel = Literal["low", "medium", "high"]

# Providers whose OpenAI-compatible surface accepts a `reasoning_effort`
# string. Shared between the AIRouter path (providers/openai_compatible.py,
# keyed by ProviderKind.value) and the builtin-agent chat loop
# (agent_loop.py, keyed by the user-configured provider string — a superset
# that also includes the "qwen" alias for DashScope/Qwen).
REASONING_EFFORT_PROVIDERS = frozenset({
    "ollama_cloud", "openai", "groq", "xai", "dashscope", "qwen", "cerebras",
})

# Anthropic extended-thinking budget table. Levels are a UX convenience over
# the raw token budget the API actually wants; "medium" (4096) intentionally
# differs from the historical flat default (2048) — see _DEFAULT_THINKING_BUDGET
# for the value used when no level is resolved (e.g. no catalog entry marks
# the model as level-capable yet), which stays exactly what it was before this
# module existed, for backward compatibility.
ANTHROPIC_THINKING_BUDGET_TOKENS: dict[str, int] = {
    "low": 1024,
    "medium": 4096,
    "high": 16384,
}
ANTHROPIC_DEFAULT_THINKING_BUDGET = 2048

# Provider kinds where the level parameter is a documented, provider-level
# wire guarantee — ANY thinking-capable model on these providers safely
# accepts a level (Anthropic: real numeric budget_tokens; the
# reasoning_effort family + OpenRouter: an optional/ignorable field per
# their own API contracts). No per-model curation needed — this class of
# provider is why the level is safe to auto-offer with zero admin/YAML
# involvement, unlike Ollama below.
LEVEL_GUARANTEED_PROVIDER_KINDS = frozenset({"anthropic", "openrouter"}) | REASONING_EFFORT_PROVIDERS


def effective_thinking_levels(
    thinking_supported: bool, provider_kind: str, curated_levels: list[str]
) -> list[str]:
    """Levels actually offered for a model in the UI/resolution.

    Explicit catalog curation (``ModelCapability.thinking_levels`` set in
    YAML or via the auto-discovery name-hint) always wins. Otherwise, for
    providers in ``LEVEL_GUARANTEED_PROVIDER_KINDS`` the level is derived
    automatically — no YAML edit, no admin action, works immediately for
    every existing thinking-capable model on that provider (e.g. Claude
    Sonnet/Haiku already in the catalog get levels the moment this function
    is read, without touching model_registry.yaml).

    Local providers (Ollama/llama.cpp/vLLM) are deliberately NOT
    auto-derived here. Empirically verified 2026-08-17 against a live
    Ollama instance: qwen3.8:27b accepts a string ``think`` level
    (HTTP 200, no type error) but does not honor it — three repeated
    ``think:true`` calls on the same prompt produced 386-419 chars of
    reasoning, and low/medium/high produced 410/430/216 — no monotonic
    trend, fully within that same sampling-noise band. Ollama being lenient
    about accepting the field is not evidence the model's template actually
    implements graduated reasoning effort; only gpt-oss is documented to.
    Offering a level selector that silently no-ops would be worse than not
    offering one, so local models stay level-less unless the (conservative)
    gpt-oss name-hint or explicit manual curation sets thinking_levels.
    """
    if curated_levels:
        return curated_levels
    if thinking_supported and provider_kind in LEVEL_GUARANTEED_PROVIDER_KINDS:
        return ["low", "medium", "high"]
    return []


def thinking_request_params(
    provider: str, thinking: bool, level: ThinkingLevel | None
) -> dict[str, Any]:
    """Provider-specific HTTP params for a resolved (thinking, level) pair.

    ``provider`` is a lowercase provider identifier (``ProviderKind.value`` or
    one of the equivalent free-form strings used by the builtin-agent config,
    e.g. "ollama", "vllm", "llamacpp", "openrouter", "openai", "groq", "xai",
    "dashscope", "qwen", "cerebras", "ollama_cloud", "anthropic").
    """
    if provider == "ollama":
        if not thinking:
            # Ollama is lenient and ignores unknown fields, so send both knobs.
            return {"think": False, "chat_template_kwargs": {"enable_thinking": False}}
        if level:
            # Ollama accepts a qualitative level string directly for models
            # that support it (currently the gpt-oss family). Callers must
            # only pass a level here when the model's catalog entry declares
            # thinking_levels — this function does not validate that itself.
            return {"think": level}
        return {"think": True}

    if provider in ("llamacpp", "vllm"):
        # The Qwen3 chat template exposes only a binary enable_thinking
        # switch — no granular level in the template itself. `level` has no
        # effect here; this is a known provider limitation, not a bug.
        return {"chat_template_kwargs": {"enable_thinking": thinking}}

    if provider == "openrouter":
        if not thinking:
            return {"reasoning": {"enabled": False}}
        return {"reasoning": {"enabled": True, **({"effort": level} if level else {})}}

    if provider in REASONING_EFFORT_PROVIDERS:
        if not thinking:
            return {"reasoning_effort": "none"}
        return {"reasoning_effort": level or "medium"}

    if provider == "anthropic":
        # Anthropic's payload shape (`thinking: {type, budget_tokens}`) is
        # built directly in providers/anthropic_provider.py — it doesn't fit
        # the flat dict-merge pattern the other branches use (max_tokens must
        # be raised alongside it). Callers for Anthropic should use
        # ANTHROPIC_THINKING_BUDGET_TOKENS/ANTHROPIC_DEFAULT_THINKING_BUDGET
        # directly instead of this function.
        return {}

    # Provider without a documented CoT knob — avoid a 400 on a strict
    # endpoint by sending nothing (matches prior behaviour for these).
    return {}
