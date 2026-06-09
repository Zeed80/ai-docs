"""Deterministic fast-path router for high-confidence count intents.

Replaces the old ad-hoc ``_try_handle_*`` cluster (which had hardcoded product
categories such as "фрез") with a single declarative, generic rule table.

Scope is intentionally narrow: only unambiguous "how many X" questions that map
to one capability list call. Everything else — tables, listings, analysis — is
left to the LLM tool-calling loop, where ``_deliver_final_content`` publishes
rich output. This keeps the router a pure speed win on weak local models
without re-introducing fragile table-building heuristics.

The router is provider/model agnostic and stateless.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def _normalize(text: str) -> str:
    return (text or "").replace("ё", "е").replace("Ё", "Е").lower().strip()


# Count-question markers. Presence of one of these is required for any match.
_COUNT_MARKERS = ("сколько", "кол-во", "количество", "число ", "всего ")

# Markers that mean the user wants a table / listing / rich output — when present
# we do NOT fast-path (the LLM flow handles those, publishing to the Workspace).
_RICH_OUTPUT_MARKERS = (
    "таблиц", "список", "перечисл", "выведи", "покажи все", "построй",
    "график", "диаграм", "excel", "экспорт", "выгруз", "по поставщик",
    "по месяц", "сгруппир", "детал", "построчн",
)

# Warehouse / stock context — required for the inventory-count rule.
_STOCK_MARKERS = ("склад", "остатк", "на складе", "в наличии", "запас")

# Price/amount phrasings: "сколько стоит", "на какую сумму" — NOT count questions.
_PRICE_MARKERS = (
    "сколько стоит", "сколько сто", "стоит", "сколько денег", "сколько рубл",
    "на какую сумму", "на сумму", "сумма счет", "сумму счет",
)


@dataclass
class FastIntent:
    """A resolved deterministic intent the executor can run without the LLM."""

    capability: str               # capability/skill map key, e.g. "invoices"
    action: str                   # capability action, e.g. "list"
    args: dict = field(default_factory=dict)
    entity_label: str = ""        # human-readable label for the answer
    search_term: str | None = None


# Top-level entity counts: keyword → (capability, label). Order matters: the most
# specific keyword must be checked first (handled by sorting on length below).
_ENTITY_COUNT_RULES: dict[str, tuple[str, str]] = {
    "счет": ("invoices", "счетов"),
    "счёт": ("invoices", "счетов"),
    "инвойс": ("invoices", "счетов"),
    "поставщик": ("suppliers", "поставщиков"),
    "контрагент": ("suppliers", "поставщиков"),
    "аномали": ("anomalies", "аномалий"),
    "документ": ("documents", "документов"),
}


def _extract_inventory_term(text: str) -> str | None:
    """Extract a generic search term for an inventory-count question.

    Generic — works for any nomenclature ("фрез", "резцы", "болты м8", ...).
    Returns None when no clear term is found.
    """
    t = _normalize(text)
    # "сколько <TERM> на складе/в наличии" — capture between marker and stock word
    m = re.search(
        r"(?:сколько|кол-во|количество)\s+(.+?)\s+(?:на\s+складе|в\s+наличии|в\s+запас\w*|остат\w*)",
        t,
    )
    if m:
        term = m.group(1).strip()
    else:
        # "остатки <TERM>" / "запас <TERM> на складе"
        m = re.search(r"(?:остатк\w*|запас\w*)\s+(.+?)(?:\s+на\s+складе|\s*[.!?]|$)", t)
        term = m.group(1).strip() if m else ""
    # Strip generic filler nouns that aren't a real search term.
    term = re.sub(r"\b(позиц\w*|товар\w*|материал\w*|тмц|номенклатур\w*|штук|шт)\b", "", term).strip()
    term = re.sub(r"\s+", " ", term).strip(" .,:;")
    return term or None


def match_fast_intent(content: str, prior_user: str | None = None) -> FastIntent | None:
    """Return a high-confidence deterministic intent, or None to defer to the LLM.

    Only fires for unambiguous count questions. Any rich-output request, or an
    ambiguous query, returns None so the normal LLM tool-calling loop runs.
    """
    t = _normalize(content)
    if not t:
        return None
    if not any(marker in t for marker in _COUNT_MARKERS):
        return None
    if any(marker in t for marker in _RICH_OUTPUT_MARKERS):
        return None
    if any(marker in t for marker in _PRICE_MARKERS):
        return None

    # ── Inventory count (stock context) ─ checked first: it is more specific. ──
    if any(marker in t for marker in _STOCK_MARKERS):
        term = _extract_inventory_term(content)
        filters: dict = {"limit": 1}
        if term:
            filters["search"] = term
        label = f"позиций на складе по запросу «{term}»" if term else "позиций на складе"
        return FastIntent(
            capability="warehouse",
            action="list_inventory",
            args={"action": "list_inventory", "filters": filters},
            entity_label=label,
            search_term=term,
        )

    # ── Top-level entity counts ──
    # Longest keyword first so "счёт"/"поставщик" beat generic substrings.
    for keyword in sorted(_ENTITY_COUNT_RULES, key=len, reverse=True):
        if keyword in t:
            capability, label = _ENTITY_COUNT_RULES[keyword]
            return FastIntent(
                capability=capability,
                action="list",
                args={"action": "list", "filters": {"limit": 1}},
                entity_label=label,
            )

    return None
