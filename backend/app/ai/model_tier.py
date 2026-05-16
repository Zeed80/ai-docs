"""Model tier routing — selects the right model based on task complexity.

Complexity tiers:
  NANO   — trivial formatting, short lookups (≤ gemma4:e4b, fast local)
  MICRO  — single-step reasoning, entity extraction (gemma4:e4b)
  SMALL  — multi-field extraction, classification (gemma4:26b or similar)
  MEDIUM — multi-step reasoning, summarisation, table planning
  LARGE  — complex planning, code generation, creative writing
  EXPERT — novel research, multi-hop reasoning (Claude API)

Confidential tasks are capped at the best available local model regardless of complexity.
"""
from __future__ import annotations

import re
from enum import IntEnum
from typing import Sequence

import structlog

logger = structlog.get_logger()

# ── Tiers ─────────────────────────────────────────────────────────────────────


class Tier(IntEnum):
    NANO = 0
    MICRO = 1
    SMALL = 2
    MEDIUM = 3
    LARGE = 4
    EXPERT = 5


# ── Keyword signals for complexity heuristic ──────────────────────────────────

_HIGH_COMPLEXITY_SIGNALS = frozenset({
    # Russian
    "сравни", "проанализируй", "объясни почему", "построй план", "разработай",
    "создай навык", "напиши код", "оптимизируй", "стратегия", "несколько шагов",
    "подробно", "исследуй", "несколько документов", "цепочку", "итого по всем",
    # English
    "analyze", "compare", "explain why", "design", "create skill", "write code",
    "optimize", "strategy", "multiple steps", "chain", "aggregate all",
})

_MEDIUM_COMPLEXITY_SIGNALS = frozenset({
    # Russian
    "таблица", "сводка", "сумма", "топ", "список всех", "отсортируй",
    "сгруппируй", "найди аномалии", "подготовь отчёт", "отчёт",
    # English
    "table", "summary", "total", "top", "list all", "sort", "group",
    "find anomalies", "prepare report", "report",
})

_LOW_COMPLEXITY_SIGNALS = frozenset({
    # Russian
    "покажи", "выведи", "статус", "сколько", "когда", "кто", "последний",
    "один", "найди счёт", "проверь",
    # English
    "show", "display", "status", "how many", "when", "who", "last", "find invoice",
    "check",
})


def score_complexity(
    text: str,
    context_tokens: int = 0,
    tool_count: int = 0,
) -> Tier:
    """Heuristically classify task complexity from user text.

    Args:
        text: User message (lower-cased before analysis).
        context_tokens: Running token count of the conversation.
        tool_count: Number of skills/tools being considered.
    """
    lower = text.lower()

    # Explicit code/skill generation → EXPERT
    if re.search(r"(создай\s+навык|напиши\s+скилл|write\s+skill|generate\s+code)", lower):
        return Tier.EXPERT

    # Score by keyword signals
    high_hits = sum(1 for kw in _HIGH_COMPLEXITY_SIGNALS if kw in lower)
    medium_hits = sum(1 for kw in _MEDIUM_COMPLEXITY_SIGNALS if kw in lower)
    low_hits = sum(1 for kw in _LOW_COMPLEXITY_SIGNALS if kw in lower)

    score = high_hits * 3 + medium_hits * 1 - low_hits * 1

    # Long context → bump up (conversation already complex)
    if context_tokens > 8_000:
        score += 2
    elif context_tokens > 4_000:
        score += 1

    # Many tools required → bump up
    if tool_count >= 5:
        score += 2
    elif tool_count >= 3:
        score += 1

    # Word count heuristic
    word_count = len(lower.split())
    if word_count > 80:
        score += 2
    elif word_count > 40:
        score += 1

    if score >= 6:
        return Tier.EXPERT
    if score >= 4:
        return Tier.LARGE
    if score >= 2:
        return Tier.MEDIUM
    if score >= 1:
        return Tier.SMALL
    if word_count <= 8:
        return Tier.NANO
    return Tier.MICRO


# ── Model tier table ───────────────────────────────────────────────────────────

# Ordered from cheapest/fastest to most capable.
# Populated from settings at runtime so the user can override via ai_settings.
_TIER_LOCAL_MODELS: dict[Tier, list[str]] = {
    Tier.NANO:   ["gemma4:e4b"],
    Tier.MICRO:  ["gemma4:e4b"],
    Tier.SMALL:  ["gemma4:e4b", "gemma4:26b"],
    Tier.MEDIUM: ["gemma4:26b"],
    Tier.LARGE:  ["gemma4:26b"],
    Tier.EXPERT: ["gemma4:26b"],
}

_TIER_CLOUD_MODELS: dict[Tier, list[str]] = {
    Tier.NANO:   ["claude-haiku-4-5"],
    Tier.MICRO:  ["claude-haiku-4-5"],
    Tier.SMALL:  ["claude-haiku-4-5"],
    Tier.MEDIUM: ["claude-sonnet-4-6"],
    Tier.LARGE:  ["claude-sonnet-4-6"],
    Tier.EXPERT: ["claude-sonnet-4-6"],
}


def select_models(
    tier: Tier,
    *,
    confidential: bool = False,
    allow_cloud: bool = True,
    preferred_local_model: str | None = None,
    preferred_cloud_model: str | None = None,
) -> list[str]:
    """Return ordered list of model names to try for the given tier.

    Local models are always tried first. Cloud models follow if allow_cloud=True
    and confidential=False.
    """
    local = list(_TIER_LOCAL_MODELS.get(tier, _TIER_LOCAL_MODELS[Tier.MEDIUM]))
    cloud = list(_TIER_CLOUD_MODELS.get(tier, _TIER_CLOUD_MODELS[Tier.MEDIUM]))

    # Override with runtime settings if available
    if preferred_local_model:
        if tier <= Tier.SMALL:
            local = [preferred_local_model]
        else:
            local = [preferred_local_model] + [m for m in local if m != preferred_local_model]

    if preferred_cloud_model:
        cloud = [preferred_cloud_model] + [m for m in cloud if m != preferred_cloud_model]

    chain = local[:]
    if allow_cloud and not confidential:
        chain.extend(cloud)

    return chain


def update_tier_models(
    local_fast: str | None = None,
    local_smart: str | None = None,
    cloud_model: str | None = None,
) -> None:
    """Update tier table from runtime ai_config. Called on config change."""
    if local_fast:
        _TIER_LOCAL_MODELS[Tier.NANO] = [local_fast]
        _TIER_LOCAL_MODELS[Tier.MICRO] = [local_fast]
        _TIER_LOCAL_MODELS[Tier.SMALL] = [local_fast, local_smart or local_fast]

    if local_smart:
        _TIER_LOCAL_MODELS[Tier.MEDIUM] = [local_smart]
        _TIER_LOCAL_MODELS[Tier.LARGE] = [local_smart]
        _TIER_LOCAL_MODELS[Tier.EXPERT] = [local_smart]

    if cloud_model:
        for tier in Tier:
            _TIER_CLOUD_MODELS[tier] = [cloud_model]

    logger.info(
        "model_tier_updated",
        local_fast=local_fast,
        local_smart=local_smart,
        cloud=cloud_model,
    )


# ── Chain-of-Draft prompt injection ───────────────────────────────────────────

_COD_SUFFIX = (
    "\n\nThink step by step but be concise: ≤6 words per reasoning step. "
    "Show steps, then give the final answer."
)

_COD_SUFFIX_RU = (
    "\n\nДумай пошагово, но кратко: ≤6 слов на шаг рассуждения. "
    "Покажи шаги, затем дай финальный ответ."
)


def inject_chain_of_draft(prompt: str, *, russian: bool = True) -> str:
    """Append Chain-of-Draft reasoning instruction to a prompt.

    Reduces token waste vs full Chain-of-Thought while retaining step clarity.
    Use for Tier.SMALL and Tier.MEDIUM tasks on weak local models.
    """
    suffix = _COD_SUFFIX_RU if russian else _COD_SUFFIX
    if suffix.strip() in prompt:
        return prompt  # already injected
    return prompt + suffix


def should_use_cod(tier: Tier, model: str) -> bool:
    """Return True if Chain-of-Draft should be injected for this tier+model combo."""
    local_models = {"gemma4:e4b", "gemma4:12b", "gemma4:26b", "gemma4:27b"}
    is_local = any(lm in model for lm in local_models)
    return is_local and tier in (Tier.MEDIUM, Tier.LARGE)
