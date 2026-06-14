"""Declarative request-routing table (aiagent/config/routes.yml).

Single source of truth for keyword routing. Consumers:

- the orchestrator's heuristic planner (``match_route`` + request-type markers),
- the deterministic fast path (``match_fast_intent`` re-exported through
  ``fast_intent_router``),
- the domain sections of the orchestrator system prompt (``prompt_sections``),
- post-turn action chips (``chips_for``).

The YAML is reloaded on mtime change (same pattern as the role-prompt cache);
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
    for route in table.get("routes") or []:
        if not route.get("intent"):
            problems.append("route without intent")
        if not route.get("keywords"):
            problems.append(f"route {route.get('intent')!r} has no keywords")
        if not route.get("role"):
            problems.append(f"route {route.get('intent')!r} has no role")
    for canvas_id, skill in (table.get("canvas_to_skill") or {}).items():
        if skill not in (table.get("skill_to_spec") or {}):
            problems.append(f"canvas {canvas_id!r} maps to {skill!r} which has no skill_to_spec")
    if problems:
        logger.warning("route_table_invalid", problems=problems)


def _markers(key: str) -> tuple[str, ...]:
    return tuple(_table().get(key) or ())


# ── Request-type markers ───────────────────────────────────────────────────────


def is_workspace_request(text: str) -> bool:
    t = normalize(text)
    return any(marker in t for marker in _markers("workspace_request_markers"))


def is_table_edit_request(text: str) -> bool:
    t = normalize(text)
    return any(marker in t for marker in _markers("table_edit_markers"))


def references_existing_table(text: str) -> bool:
    t = normalize(text)
    return any(marker in t for marker in _markers("existing_table_markers"))


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
        return False
    return True


# ── Intent routes ──────────────────────────────────────────────────────────────


def match_route(text: str) -> dict[str, Any] | None:
    """Return the first route whose keywords match the normalized text."""
    t = normalize(text)
    for route in _table().get("routes") or []:
        if any(kw in t for kw in route.get("keywords") or []):
            return route
    return None


def resolve_canvas_from_route(route: dict[str, Any], text: str) -> str | None:
    """Walk canvas_rules of the route to pick a canvas for the given text."""
    t = normalize(text)
    for rule in route.get("canvas_rules") or []:
        if any(kw in t for kw in rule.get("require_any") or []):
            for sub in rule.get("sub_rules") or []:
                if any(kw in t for kw in sub.get("require_any") or []):
                    return sub["canvas_id"]
            return rule["canvas_id"]
    return route.get("default_canvas")


def fallback_canvas(text: str) -> str | None:
    """Pick a canvas via fallback_canvas_rules when no route resolved one."""
    t = normalize(text)
    for rule in _table().get("fallback_canvas_rules") or []:
        if not any(kw in t for kw in rule.get("require_any") or []):
            continue
        if rule.get("require_table_edit") and not is_table_edit_request(text):
            continue
        if rule.get("require_extra_any") and not any(
            kw in t for kw in rule["require_extra_any"]
        ):
            continue
        for sub in rule.get("sub_rules") or []:
            if any(kw in t for kw in sub.get("require_any") or []):
                return sub["canvas_id"]
        return rule["canvas_id"]
    return None


# ── Supplier helpers ───────────────────────────────────────────────────────────

_SUPPLIER_NAME_STOPWORDS = frozenset({
    # Russian stopwords
    "всех", "всем", "всеми", "всё", "все", "другим", "другие", "другого",
    "любой", "каждый", "каждого", "одного", "один", "без", "только",
    "кроме", "нескольких", "нескольким", "поставщикам", "поставщиках",
    "лучший", "лучшего", "лучшем", "худший", "первый", "последний",
    # Short prepositions / function words (frequently mismatched)
    "по", "из", "от", "до", "за", "на", "об", "во", "ко", "со", "для",
    "при", "над", "под", "про", "как", "что", "или", "это", "тот", "тем",
    # English words that may appear after "поставщик"
    "trust", "score", "rating", "list", "profile", "top", "best",
})

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


def supplier_grouping() -> dict[str, Any]:
    return dict(_table().get("supplier_grouping") or {})


def is_supplier_grouping_request(text: str) -> bool:
    sg = supplier_grouping()
    t = normalize(text)
    has_trigger = any(kw in t for kw in sg.get("trigger_keywords") or [])
    has_items = any(kw in t for kw in sg.get("item_keywords") or [])
    if not (has_trigger and has_items):
        return False
    # A specific supplier name makes this a filter request, not a group-by.
    return extract_supplier_name(text) is None


# ── Canvas/skill mappings (audit + direct repair) ──────────────────────────────


def canvas_to_skill(canvas_id: str | None) -> str | None:
    return (_table().get("canvas_to_skill") or {}).get(canvas_id or "")


def skill_spec(skill: str) -> dict[str, Any] | None:
    return (_table().get("skill_to_spec") or {}).get(skill)


def capability_output_type(capability: str) -> str | None:
    return (_table().get("capability_output_types") or {}).get(capability)


# ── Fast path: deterministic count intents (0 LLM) ─────────────────────────────


@dataclass
class FastIntent:
    """A resolved deterministic intent the executor can run without the LLM."""

    capability: str               # capability/skill map key, e.g. "invoices"
    action: str                   # capability action, e.g. "list"
    args: dict = field(default_factory=dict)
    entity_label: str = ""        # human-readable label for the answer
    search_term: str | None = None


_INVENTORY_TERM_RE = re.compile(
    r"(?:сколько|кол-во|количество)\s+(.+?)\s+(?:на\s+складе|в\s+наличии|в\s+запас\w*|остат\w*)"
)
_INVENTORY_TERM_FALLBACK_RE = re.compile(
    r"(?:остатк\w*|запас\w*)\s+(.+?)(?:\s+на\s+складе|\s*[.!?]|$)"
)
_INVENTORY_FILLER_RE = re.compile(
    r"\b(позиц\w*|товар\w*|материал\w*|тмц|номенклатур\w*|штук|шт)\b"
)


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
    if any(marker in t for marker in fp.get("price_markers") or []):
        return None

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


def prompt_sections() -> str:
    """Domain sections rendered into the orchestrator system prompt."""
    parts: list[str] = []
    for section in _table().get("prompt_sections") or []:
        title = str(section.get("title") or "").strip()
        text = str(section.get("text") or "").strip()
        if title and text:
            parts.append(f"{title}:\n  {text}")
    return "\n\n".join(parts)
