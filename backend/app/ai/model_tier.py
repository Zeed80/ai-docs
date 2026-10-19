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

_HIGH_COMPLEXITY_SIGNALS = frozenset(
    {
        # Russian
        "сравни",
        "проанализируй",
        "объясни почему",
        "построй план",
        "разработай",
        "создай навык",
        "напиши код",
        "оптимизируй",
        "стратегия",
        "несколько шагов",
        "подробно",
        "исследуй",
        "несколько документов",
        "цепочку",
        "итого по всем",
        # English
        "analyze",
        "compare",
        "explain why",
        "design",
        "create skill",
        "write code",
        "optimize",
        "strategy",
        "multiple steps",
        "chain",
        "aggregate all",
    }
)

_MEDIUM_COMPLEXITY_SIGNALS = frozenset(
    {
        # Russian
        "таблица",
        "сводка",
        "сумма",
        "топ",
        "список всех",
        "отсортируй",
        "сгруппируй",
        "найди аномалии",
        "подготовь отчёт",
        "отчёт",
        # Analytical / exploratory queries that need LLM planning
        "расскажи",
        "расскажи о",
        "популярн",
        "больше всего",
        "чаще всего",
        "самые",
        "какие",
        "почему",
        "как так",
        "что значит",
        "разбери",
        "объясни",
        "покажи динамику",
        "за период",
        "тенденц",
        "тренд",
        "статистик",
        "аналитик",
        "по месяц",
        "по поставщик",
        "сгруппир",
        "группировк",
        "номенклатур",
        "какая",
        "каких",
        "насколько",
        # English
        "table",
        "summary",
        "total",
        "top",
        "list all",
        "sort",
        "group",
        "find anomalies",
        "prepare report",
        "report",
        "explain",
        "popular",
        "most common",
        "trending",
        "analytics",
        "by month",
        "by supplier",
        "breakdown",
        "distribution",
    }
)

_LOW_COMPLEXITY_SIGNALS = frozenset(
    {
        # Russian — только однозначно тривиальные навигационные запросы без фильтров
        "статус",
        "когда",
        "кто",
        "последний",
        "один",
        "найди счёт",
        "проверь",
        # English
        "status",
        "when",
        "who",
        "last",
        "find invoice",
        "check",
        # NOTE: "покажи", "выведи", "сколько" убраны:
        #   "покажи"/"выведи" — могут вести к workspace-запросу
        #   "сколько" — фильтрованные count-вопросы ("сколько ожидают утверждения?")
        #   требуют API-вызова; оставлять на fast-path только чистые "сколько всего" через
        #   secretary snapshot (flow_status_markers уже ловят их раньше)
    }
)


# Action verbs: the user wants something DONE to a record (check arithmetic,
# approve, send…), not a table shown. Such turns must reach the worker even when
# an entity resolves a workspace canvas — otherwise a proactive table silently
# replaces the requested action.
_ACTION_SIGNALS = frozenset(
    {
        "проверь",
        "проверить",
        "перепровер",
        "пересчита",
        "пересчёт",
        "пересчет",
        "утверди",
        "утвердить",
        "одобри",
        "отклони",
        "отклонить",
        "отправь",
        "отправить",
        "удали",
        "удалить",
        "оплати",
        "оплатить",
        "подтверди",
        "подтвердить",
        "валидир",
        "исправь",
        "исправить",
        "обнови",
        "обновить",
        "измени",
        "изменить",
        "согласуй",
        "согласовать",
        "заполни",
        "создай счёт",
        "проведи",
        "разнеси",
        "верифицир",
        # Ingest/attach verbs: "загрузи каталог", "прикрепи прайс к поставщику".
        # Without these a request to PUT data somewhere resolved a supplier canvas
        # and was answered with a table of what was already in the DB (live finding).
        "загрузи",
        "загрузить",
        "прикрепи",
        "прикрепить",
        "привяжи",
        "привязать",
        "импортируй",
        "импортировать",
        "скачай",
        "скачать",
        "добавь в каталог",
        # English
        "validate",
        "approve",
        "reject",
        "send",
        "delete",
        "pay",
        "confirm",
        "verify",
        "fix",
        "update record",
    }
)


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
    # NOTE: filtered count questions ("сколько счетов ожидают утверждения")
    # are handled deterministically by the status_counts fast-path (0 LLM) —
    # they must stay at low tier so the executor fast-path fires instead of
    # forcing model planning. (Previously bumped to SMALL via a workaround.)

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


def aux_quality_budget(tier: Tier) -> int:
    """Max auxiliary quality LLM calls per turn (semantic audit, self-refine).

    Planner and executor calls are structural (already tier-gated); retries are
    capped by ``max_audit_retries``. This budget bounds the *extra* checking
    layers, which dominate latency on local models: below LARGE only one
    diagnostic call is allowed (semantic audit after a failed mechanical
    audit), at LARGE+ there is room for semantic audit plus one refine pass.

    Thresholds live in routes.yml (aux_quality_budgets, A4) — fallback
    literals match the pre-A4 hardcoded values.
    """
    from app.ai import route_table

    budgets = route_table.aux_quality_budgets()
    default = int(budgets.get("default", 1))
    if tier >= Tier.LARGE:
        return int(budgets.get("large", 2))
    return default


# ── Model tier table ───────────────────────────────────────────────────────────

# Ordered from cheapest/fastest to most capable.
# Populated from settings at runtime so the user can override via ai_settings.
_TIER_LOCAL_MODELS: dict[Tier, list[str]] = {
    Tier.NANO: ["gemma4:e4b"],
    Tier.MICRO: ["gemma4:e4b"],
    Tier.SMALL: ["gemma4:e4b", "gemma4:26b"],
    Tier.MEDIUM: ["gemma4:26b"],
    Tier.LARGE: ["gemma4:26b"],
    Tier.EXPERT: ["gemma4:26b"],
}

_TIER_CLOUD_MODELS: dict[Tier, list[str]] = {
    Tier.NANO: ["claude-haiku-4-5"],
    Tier.MICRO: ["claude-haiku-4-5"],
    Tier.SMALL: ["claude-haiku-4-5"],
    Tier.MEDIUM: ["claude-sonnet-4-6"],
    Tier.LARGE: ["claude-sonnet-4-6"],
    Tier.EXPERT: ["claude-sonnet-4-6"],
}


# ── Chain-of-Draft prompt injection ───────────────────────────────────────────

_COD_SUFFIX = (
    "\n\nThink step by step but be concise: ≤6 words per reasoning step. "
    "Show steps, then give the final answer."
)

_COD_SUFFIX_RU = (
    "\n\nДумай пошагово, но кратко: ≤6 слов на шаг рассуждения. "
    "Покажи шаги, затем дай финальный ответ."
)
