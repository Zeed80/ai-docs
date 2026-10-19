"""Declarative request-routing table (aiagent/config/routes.yml).

Single source of truth for the keywords of the paths that run without the
model. Consumers:

- the deterministic fast path (``match_fast_intent`` re-exported through
  ``fast_intent_router``) and the flow-status / retrieval markers,
- post-turn action chips (``chips_for``),
- thresholds and word lists the turn reads (response budgets, field terms).

The turn itself is not routed: one employee with one prompt takes every
request that no fast path answered.

The YAML is reloaded on mtime change;
``invalidate_cache`` forces a reload on the next access (wired to the Redis
``skill_reload`` event).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from app.ai.degradation import log_degraded

logger = structlog.get_logger()


def _routes_path() -> Path:
    # Same root resolution as gateway_config (AIAGENT_ROOT env override in Docker).
    from app.ai.gateway_config import _AIAGENT_ROOT

    return _AIAGENT_ROOT / "config" / "routes.yml"


_cache: dict[str, Any] | None = None
_cache_mtime: float = 0.0


def normalize(text: str) -> str:
    """Normalization contract for every keyword comparison: lower(), ё→е."""
    return (text or "").lower().replace("ё", "е").strip()


def invalidate_cache() -> None:
    """Force reload of routes.yml on next access (Redis skill_reload event)."""
    global _cache, _cache_mtime
    _cache = None
    _cache_mtime = 0.0


def _table() -> dict[str, Any]:
    global _cache, _cache_mtime
    try:
        path = _routes_path()
        mtime = path.stat().st_mtime
        if _cache is None or mtime != _cache_mtime:
            _cache = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            _cache_mtime = mtime
            _validate(_cache)
    except Exception as exc:
        log_degraded("route_table.load", exc)
        if _cache is None:
            _cache = {}
    return _cache


def _validate(table: dict[str, Any]) -> None:
    """Fail fast on structural mistakes a YAML edit can introduce."""
    problems: list[str] = []
    for name, spec in (table.get("skill_to_spec") or {}).items():
        if not isinstance(spec, dict) or not spec.get("path"):
            problems.append(f"skill {name!r} has no path")
    if problems:
        logger.warning("route_table_invalid", problems=problems)


def _markers(key: str) -> tuple[str, ...]:
    return tuple(_table().get(key) or ())


# ── Request-type markers ───────────────────────────────────────────────────────


def is_workspace_request(text: str) -> bool:
    t = normalize(text)
    return any(marker in t for marker in _markers("workspace_request_markers"))


def is_flow_status_query(text: str) -> bool:
    """Cross-cutting document-flow status question (secretary scope)."""
    t = normalize(text)
    return any(marker in t for marker in _markers("flow_status_markers"))


def needs_document_retrieval(text: str) -> bool:
    """Decide whether a turn should run RAG (document chunks / long-term memory).

    Skips the costly memory.search (vector + cross-encoder reranker) for pure
    workspace/flow queries that are answered straight from SQL — they gain
    nothing from semantic chunk recall and only pay latency + context noise.
    Content markers ("о чём", "напомни", "найди похожий"…) force retrieval on,
    even when the query is otherwise workspace-shaped. Everything else defaults
    to retrieval — episodic/factual context is cheap insurance for open chat.
    """
    t = normalize(text)
    if any(marker in t for marker in _markers("retrieval_content_markers")):
        return True
    if is_flow_status_query(text) or is_workspace_request(text):
        # Override: a workspace/flow-shaped query that's actually asking about
        # a RELATIONSHIP ("что связано с этим поставщиком") needs graph
        # traversal, not a table/status answer — don't skip retrieval for it.
        if any(marker in t for marker in _markers("relational_markers")) and any(
            marker in t for marker in _markers("entity_domain_markers")
        ):
            return True
        return False
    return True


# ── Intent routes ──────────────────────────────────────────────────────────────


# ── Supplier helpers ───────────────────────────────────────────────────────────

_SUPPLIER_NAME_STOPWORDS = frozenset(
    {
        # Russian stopwords
        "всех",
        "всем",
        "всеми",
        "всё",
        "все",
        "другим",
        "другие",
        "другого",
        "любой",
        "каждый",
        "каждого",
        "одного",
        "один",
        "без",
        "только",
        "кроме",
        "нескольких",
        "нескольким",
        "поставщикам",
        "поставщиках",
        "лучший",
        "лучшего",
        "лучшем",
        "худший",
        "первый",
        "последний",
        # Short prepositions / function words (frequently mismatched)
        "по",
        "из",
        "от",
        "до",
        "за",
        "на",
        "об",
        "во",
        "ко",
        "со",
        "для",
        "при",
        "над",
        "под",
        "про",
        "как",
        "что",
        "или",
        "это",
        "тот",
        "тем",
        # English words that may appear after "поставщик"
        "trust",
        "score",
        "rating",
        "list",
        "profile",
        "top",
        "best",
    }
)

_SUPPLIER_NAME_RE = re.compile(
    r"поставщик[аиу]?\s+([«»\"']?[а-яёa-zА-ЯЁA-Z][а-яёa-z0-9А-ЯЁA-Z«»\"'\-\.]{2,}"
    r"(?:\s+[а-яёa-z0-9А-ЯЁA-Z«»\"'\-\.]{2,}){0,3}[«»\"']?)",
    re.IGNORECASE,
)


def extract_supplier_name(text: str) -> str | None:
    """Extract a specific named supplier from user text.

    Only matches proper-noun patterns (quoted, ALL-CAPS, or title-case words of
    meaningful length). Returns None for generic attribute requests like
    "поставщик по trust score" or "лучший поставщик".
    """
    m = _SUPPLIER_NAME_RE.search(text or "")
    if m:
        name = m.group(1).strip().strip("«»\"'.,;:!?")
        if name.lower() not in _SUPPLIER_NAME_STOPWORDS and len(name) >= 3:
            return name
    return None


# ── Canvas/skill mappings (audit + direct repair) ──────────────────────────────


def skill_spec(skill: str) -> dict[str, Any] | None:
    return (_table().get("skill_to_spec") or {}).get(skill)


def response_budgets() -> dict[str, Any]:
    """Per-turn response token budget thresholds (A4) — see routes.yml."""
    return _table().get("response_budgets") or {}


def stale_source_markers() -> list[str]:
    """T8b: phrases that mark a web page as out of date."""
    return list(_table().get("stale_source_markers") or [])


def group_request_words() -> list[str]:
    """T7b: words that make a request ask for a whole set of records."""
    return list(_table().get("group_request_words") or [])


def request_date_words() -> list[str]:
    """T8i: word stems by which a request names its own date ("в понедельник",
    "через неделю", "15 ноября") — a reminder's date is then the user's."""
    return list(_table().get("request_date_words") or [])


def field_terms(tool: str) -> dict[str, list[str]]:
    """T7c: field → word stems the request must contain to change it."""
    return (_table().get("field_terms") or {}).get(tool) or {}


def aux_quality_budgets() -> dict[str, Any]:
    """Max auxiliary quality LLM calls per turn thresholds (A4) — see routes.yml."""
    return _table().get("aux_quality_budgets") or {}


# ── Fast path: deterministic count intents (0 LLM) ─────────────────────────────


@dataclass
class FastIntent:
    """A resolved deterministic intent the executor can run without the LLM."""

    capability: str  # capability/skill map key, e.g. "invoices"
    action: str  # capability action, e.g. "list"
    args: dict = field(default_factory=dict)
    entity_label: str = ""  # human-readable label for the answer
    search_term: str | None = None


_INVENTORY_TERM_RE = re.compile(
    r"(?:сколько|кол-во|количество)\s+(.+?)\s+(?:на\s+складе|в\s+наличии|в\s+запас\w*|остат\w*)"
)
_INVENTORY_TERM_FALLBACK_RE = re.compile(
    r"(?:остатк\w*|запас\w*)\s+(.+?)(?:\s+на\s+складе|\s*[.!?]|$)"
)
_INVENTORY_FILLER_RE = re.compile(r"\b(позиц\w*|товар\w*|материал\w*|тмц|номенклатур\w*|штук|шт)\b")


def _extract_inventory_term(text: str) -> str | None:
    """Extract a generic search term for an inventory-count question."""
    t = normalize(text)
    m = _INVENTORY_TERM_RE.search(t)
    if m:
        term = m.group(1).strip()
    else:
        m = _INVENTORY_TERM_FALLBACK_RE.search(t)
        term = m.group(1).strip() if m else ""
    term = _INVENTORY_FILLER_RE.sub("", term).strip()
    term = re.sub(r"\s+", " ", term).strip(" .,:;")
    return term or None


_WORD_RE = re.compile(r"[a-zа-я0-9-]+")


def _unexplained_words(t: str, explained: list[str], filler: set[str]) -> list[str]:
    """Words of the question the fast path would drop.

    A count answers only «сколько <сущность> [<статус>]». Any other word — a
    supplier name, a period, an amount — is a condition the fast path cannot
    express, and answering without it gives a confident wrong number («сколько
    счетов Хоффман?» → «Всего счетов: 153», live 2026-10-13). A blacklist of
    condition words was never complete, so the check is the other way round:
    every word must be accounted for.
    """
    explained = [part for marker in explained for part in marker.split() if len(part) >= 3]
    left = []
    for word in _WORD_RE.findall(t):
        if len(word) < 3 or word in filler:
            continue
        if any(marker and marker in word for marker in explained):
            continue
        left.append(word)
    return left


def match_fast_intent(content: str, prior_user: str | None = None) -> FastIntent | None:
    """Return a high-confidence deterministic intent, or None to defer to the LLM.

    Only fires for unambiguous count questions. Any rich-output request, or an
    ambiguous query, returns None so the normal LLM tool-calling loop runs.
    """
    fp = _table().get("fast_paths") or {}
    t = normalize(content)
    if not t:
        return None
    if not any(marker in t for marker in fp.get("count_markers") or []):
        return None
    if any(marker in t for marker in fp.get("rich_output_markers") or []):
        return None
    if any(normalize(marker) in t for marker in fp.get("condition_markers") or []):
        return None
    # A second question: "сколько … и сколько …" ("сколько всего" is one).
    if t.count("сколько") > 1:
        return None
    if any(marker in t for marker in fp.get("price_markers") or []):
        return None

    filler = {normalize(str(w)) for w in fp.get("count_filler_words") or []}
    count_words = [normalize(str(m)).strip() for m in fp.get("count_markers") or []]

    # ── Inventory count (stock context) ─ checked first: it is more specific. ──
    if any(marker in t for marker in fp.get("stock_markers") or []):
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

    # ── Status-filtered counts ── checked before plain entity counts: a
    # question like "сколько счетов ожидают утверждения" must count only the
    # matching status, not the whole table. First matching rule wins (order in
    # routes.yml disambiguates "на утверждении"=needs_review from
    # "утверждён"=approved). The status goes into search_term too, so the
    # result cache keys per-status (no collision with the unfiltered count).
    for rule in fp.get("status_counts") or []:
        entity_kw = normalize(str(rule.get("entity") or ""))
        markers = [normalize(str(m)) for m in (rule.get("markers") or [])]
        if entity_kw and entity_kw in t and any(m in t for m in markers):
            if _unexplained_words(t, [*count_words, entity_kw, *markers], filler):
                return None
            status = str(rule.get("status") or "")
            return FastIntent(
                capability=str(rule.get("capability") or ""),
                action="list",
                args={"action": "list", "filters": {"status": status, "limit": 1}},
                entity_label=str(rule.get("label") or ""),
                search_term=status,
            )

    # ── Top-level entity counts ──
    # Longest keyword first so the most specific entity wins.
    rules = sorted(
        fp.get("entity_counts") or [],
        key=lambda rule: len(str(rule.get("keyword") or "")),
        reverse=True,
    )
    for rule in rules:
        keyword = normalize(str(rule.get("keyword") or ""))
        if keyword and keyword in t:
            if _unexplained_words(t, [*count_words, keyword], filler):
                return None
            return FastIntent(
                capability=str(rule.get("capability") or ""),
                action="list",
                args={"action": "list", "filters": {"limit": 1}},
                entity_label=str(rule.get("label") or ""),
            )

    return None


# ── Chips & prompt sections ────────────────────────────────────────────────────


def chips_for(intent: str, text: str, *, workspace_required: bool = False) -> list[dict]:
    """Contextual action chips for the completed turn (max 4)."""
    t = normalize(text)
    chips: list[dict] = []
    for rule in _table().get("chips") or []:
        intents = set(rule.get("intents") or [])
        keywords = rule.get("keywords") or []
        if intent in intents or any(kw in t for kw in keywords):
            for item in rule.get("items") or []:
                chips.append(dict(item))
    if workspace_required:
        chips.append({"label": "Рабочий стол", "action": "navigate", "target": "/"})
    return chips[:4]
