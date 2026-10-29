"""W4: the employee knows what it is looking for on the web.

Two steps the quick ``research`` did not have:

* :func:`make_brief` — before searching, the model states the goal, the
  entities, the facts needed (with units), which sources to trust first and
  the queries per angle and language. A question too vague to search is
  reported as ``must_clarify`` with its ambiguities; the agent asks.
* :func:`deep_research` — rounds of search → read → extract facts with a
  quote → find the gaps → search the gaps and follow the links the pages
  themselves point to (a datasheet PDF, "Документация"), until the brief is
  covered or the budget ends.

What the model says is checked, not trusted: a fact counts only if its quote
is found in the page text and its numbers are in the quote; a source's kind
(manufacturer, distributor, …) is the model's reading of the page, and the
owner's source registry (approved / rejected web sources) outranks it. Values
of one fact from several sources are compared: confirmed by two independent
sites, single, or a conflict shown with every value — never averaged away.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger()

SOURCE_KINDS = (
    "manufacturer",
    "official_distributor",
    "distributor",
    "marketplace",
    "catalog",
    "directory",
    "forum",
    "news",
    "reference",
    "unknown",
)
# The order sources are shown and preferred in when the brief names none.
_DEFAULT_PREFERENCE = (
    "manufacturer",
    "official_distributor",
    "reference",
    "distributor",
    "catalog",
    "marketplace",
    "news",
    "forum",
    "directory",
    "unknown",
)


# ── model I/O schemas (every field required: a schema default lets a model
#    drop the field and the answer still validates) ─────────────────────────


class FactNeeded(BaseModel):
    name: str = Field(description="что именно нужно, коротко: «подача», «напор», «масса»")
    unit: str | None = Field(description="единица, в которой нужно значение, или null")
    why: str | None = Field(description="зачем это нужно, если не очевидно, или null")


class BriefQuery(BaseModel):
    text: str = Field(description="поисковый запрос без внутренних данных предприятия")
    language: str = Field(description="язык запроса: ru, en, de, zh …")
    site: str | None = Field(description="домен, если искать нужно на одном сайте, или null")


class ResearchBrief(BaseModel):
    goal: str = Field(description="что нужно найти, одним предложением")
    entities: list[str] = Field(description="изделие, модель, артикул, производитель, марка")
    facts_needed: list[FactNeeded]
    source_preference: list[str] = Field(
        description=f"типы источников по убыванию доверия из: {', '.join(SOURCE_KINDS)}"
    )
    queries: list[BriefQuery] = Field(description="2–6 запросов с разных сторон и на разных языках")
    stop_when: str = Field(description="когда найденного достаточно")
    ambiguities: list[str] = Field(description="что в вопросе можно понять по-разному")
    must_clarify: bool = Field(
        description="true, только если без уточнения искать бессмысленно (непонятно, что за изделие)"
    )


class PageFact(BaseModel):
    name: str = Field(description="имя факта из списка нужных")
    value: str = Field(description="значение как на странице, с числом")
    unit: str | None
    quote: str = Field(description="дословная цитата со страницы, где стоит это значение")


class PageLinkPick(BaseModel):
    url: str = Field(description="адрес из списка ссылок страницы")
    why: str


class PageReading(BaseModel):
    kind: str = Field(description=f"тип источника: {', '.join(SOURCE_KINDS)}")
    kind_reason: str = Field(description="по чему это видно на странице")
    about_entity: bool = Field(description="страница именно про искомое изделие")
    facts: list[PageFact]
    links: list[PageLinkPick] = Field(
        description="до 3 ссылок этой страницы, где вероятно есть недостающее (паспорт, PDF, документация)"
    )


# ── the model ────────────────────────────────────────────────────────────────


def web_model_assigned() -> bool:
    """Has the operator assigned the «Веб-исследование» slot?"""
    from app.ai.schemas import AITask
    from app.ai.task_routing import _redis_get

    stored = (_redis_get() or {}).get(AITask.WEB_RESEARCH.value) or {}
    return bool(stored.get("models"))


def _preferred_model() -> str | None:
    """None → the slot's own model; otherwise the employee's model by name."""
    if web_model_assigned():
        return None
    from app.ai.agent_config import get_builtin_agent_config
    from app.ai.orchestrator import _registry_model_name

    config = get_builtin_agent_config()
    return _registry_model_name(config.worker_model or config.model)


class ResearchModelError(RuntimeError):
    """The model did not answer in the schema (or the call failed)."""


async def _ask(schema: type[BaseModel], system: str, user: str, *, max_tokens: int) -> Any:
    from app.ai.router import ai_router
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    try:
        response = await ai_router.run(
            AIRequest(
                task=AITask.WEB_RESEARCH,
                messages=[
                    ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=user),
                ],
                response_schema=schema,
                preferred_model=_preferred_model(),
                thinking=False,
                max_output_tokens=max_tokens,
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise ResearchModelError(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
    data = response.data
    try:
        return data if isinstance(data, schema) else schema.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        raise ResearchModelError(f"schema: {str(exc)[:200]}") from exc


# ── the brief ────────────────────────────────────────────────────────────────

_BRIEF_SYSTEM = (
    "Ты планируешь поиск в интернете для сотрудника промышленного предприятия. "
    "Разбери вопрос: что именно нужно найти, про какое изделие или материал, какие "
    "конкретные факты (с единицами) ответят на вопрос, каким источникам верить "
    "первыми (сайт и паспорт производителя важнее магазина и форума). Составь "
    "запросы с разных сторон: точное название модели, артикул, «паспорт», "
    "«технические характеристики», «datasheet», на русском и, если изделие "
    "иностранное, на языке производителя. Если домен производителя известен, "
    "дай отдельный запрос с site. В запросы НЕ включай внутренние данные "
    "предприятия: цены и суммы, ИНН, номера счетов и договоров, имена и почту "
    "сотрудников. Отвечай только JSON по схеме."
)

# What must not leave in a query: e-mail, phone, INN, card-like numbers, sums.
_LEAKS = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("phone", re.compile(r"(?:\+7|\b8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b")),
    ("inn", re.compile(r"\b\d{10}(?:\d{2})?\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){16}\b")),
    ("amount", re.compile(r"\d[\d\s.,]*\s?(?:руб|₽|rub\b|usd|\$|eur|€)", re.I)),
)


def query_leaks(text: str) -> list[str]:
    return [name for name, pattern in _LEAKS if pattern.search(text or "")]


def _bound_brief(brief: ResearchBrief) -> tuple[ResearchBrief, list[str]]:
    """Trim to sane sizes and drop queries that would carry company data."""
    diagnostics: list[str] = []
    kept = []
    for query in brief.queries[:8]:
        leaks = query_leaks(query.text)
        if leaks:
            diagnostics.append(f"query_leak_dropped:{','.join(leaks)}")
            continue
        if query.site:
            query.site = query.site.strip().lower().removeprefix("www.").split("/")[0] or None
        kept.append(query)
    preference = [k for k in brief.source_preference if k in SOURCE_KINDS] or list(
        _DEFAULT_PREFERENCE
    )
    bounded = brief.model_copy(
        update={
            "facts_needed": brief.facts_needed[:12],
            "queries": kept,
            "entities": brief.entities[:8],
            "source_preference": preference,
            "ambiguities": brief.ambiguities[:6],
        }
    )
    return bounded, diagnostics


async def make_brief(question: str, context: str | None = None) -> tuple[ResearchBrief, list[str]]:
    today = datetime.now(UTC).date().isoformat()
    user = f"Сегодня {today}.\nВопрос: {question.strip()}"
    if context:
        user += (
            f"\nЧто уже известно (не для запросов, только чтобы понять вопрос): {context[:1500]}"
        )
    brief = await _ask(ResearchBrief, _BRIEF_SYSTEM, user, max_tokens=1500)
    bounded, diagnostics = _bound_brief(brief)
    if not bounded.queries and not bounded.must_clarify:
        # Every query carried company data, or none came back: search the goal.
        fallback = " ".join([*bounded.entities[:3]] or [bounded.goal])[:200]
        if not query_leaks(fallback):
            bounded.queries = [BriefQuery(text=fallback, language="ru", site=None)]
    return bounded, diagnostics


# ── reading one page ─────────────────────────────────────────────────────────

_READ_SYSTEM = (
    "Ты читаешь одну веб-страницу для сотрудника предприятия. Выпиши только те "
    "нужные факты, которые на странице ЕСТЬ, со значением как на странице и "
    "ДОСЛОВНОЙ цитатой (кусок текста страницы, где стоит значение, 5–40 слов). "
    "Не вычисляй, не подставляй из памяти, не бери значение другой модели или "
    "размера: если страница про другое исполнение — about_entity=false. "
    "Определи тип источника по самой странице (кто её публикует). Из ссылок "
    "страницы выбери до 3, где вероятно есть недостающее. Отвечай только JSON."
)

# Characters of the page the model reads per call (passages around the facts).
_READ_BUDGET = 6000


def _facts_list(brief: ResearchBrief, only: list[str] | None = None) -> str:
    rows = []
    for fact in brief.facts_needed:
        if only is not None and fact.name not in only:
            continue
        rows.append(f"- {fact.name}" + (f" ({fact.unit})" if fact.unit else ""))
    return "\n".join(rows) or "- (факты не перечислены — выпиши характеристики изделия)"


def _norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е").replace(" ", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> list[float]:
    return [float(n.replace(",", ".")) for n in _NUMBER.findall((text or "").replace(" ", ""))]


def verify_fact(fact: PageFact, page_text: str) -> str | None:
    """None when the fact stands on the page; else why it was dropped."""
    quote = _norm(fact.quote)
    if len(quote) < 8:
        return "quote_too_short"
    body = _norm(page_text)
    if quote not in body:
        # Models trim punctuation at the edges; accept the quote's core.
        core = quote.strip(" .,;:«»\"'()-")
        if len(core) < 8 or core not in body:
            return "quote_not_on_page"
    value_numbers = _numbers(fact.value)
    if value_numbers:
        quote_numbers = _numbers(fact.quote)
        if not all(any(abs(v - q) < 1e-9 for q in quote_numbers) for v in value_numbers):
            return "value_not_in_quote"
    return None


async def read_page(
    brief: ResearchBrief,
    page_text: str,
    page_url: str,
    title: str | None,
    links: list[dict],
    missing: list[str] | None,
) -> PageReading:
    from app.api.web_search import extract_passages

    terms = " ".join([*brief.entities, *(f.name for f in brief.facts_needed)])
    passages = extract_passages(page_text, terms, _READ_BUDGET, max_n=6)
    excerpt = "\n…\n".join(p.text for p in passages) or page_text[:_READ_BUDGET]
    link_rows = "\n".join(
        f"- {item.get('text', '')[:80]} → {item['url']}" for item in links[:40] if item.get("url")
    )
    user = (
        f"Ищем: {brief.goal}\nИзделие: {', '.join(brief.entities) or '—'}\n"
        f"Нужные факты:\n{_facts_list(brief, missing)}\n\n"
        f"Страница: {title or ''} {page_url}\nТекст страницы (фрагменты):\n{excerpt}\n\n"
        f"Ссылки страницы:\n{link_rows or '—'}"
    )
    return await _ask(PageReading, _READ_SYSTEM, user, max_tokens=1800)


# ── agreement between sources ────────────────────────────────────────────────


def _same_value(a: str, b: str) -> bool:
    na, nb = _numbers(a), _numbers(b)
    if na and nb:
        return len(na) == len(nb) and all(
            abs(x - y) <= 0.01 * max(abs(x), abs(y), 1e-9) for x, y in zip(na, nb, strict=True)
        )
    return _norm(a) == _norm(b)


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def aggregate_facts(brief: ResearchBrief, found: list[dict]) -> list[dict]:
    """One row per needed fact: values grouped, sources counted by site."""
    names = [f.name for f in brief.facts_needed] or sorted({f["name"] for f in found})
    units = {f.name: f.unit for f in brief.facts_needed}
    rows = []
    for name in names:
        groups: list[dict] = []
        for item in (f for f in found if f["name"] == name):
            group = next((g for g in groups if _same_value(g["value"], item["value"])), None)
            if group is None:
                group = {
                    "value": item["value"],
                    "unit": item.get("unit"),
                    "sources": [],
                    "quotes": [],
                }
                groups.append(group)
            if item["n"] not in group["sources"]:
                group["sources"].append(item["n"])
                if len(group["quotes"]) < 2:  # the history keeps a short answer
                    group["quotes"].append(item["quote"][:200])
            group.setdefault("_domains", set()).add(item["domain"])
        for group in groups:
            group["sites"] = len(group.pop("_domains", set()))
        groups.sort(key=lambda g: -g["sites"])
        if not groups:
            status = "missing"
        elif len(groups) > 1:
            status = "conflict"
        elif groups[0]["sites"] >= 2:
            status = "confirmed"
        else:
            status = "single"
        rows.append({"name": name, "unit": units.get(name), "status": status, "values": groups})
    return rows


# ── the owner's source registry ──────────────────────────────────────────────


async def registry_domains() -> tuple[set[str], set[str]]:
    """(approved, rejected) domains of the web-source registry; empty if unavailable."""
    try:
        from sqlalchemy import select

        from app.db.models import MemoryFact
        from app.db.session import _get_session_factory

        async with _get_session_factory()() as db:
            rows = (
                await db.execute(
                    select(MemoryFact).where(MemoryFact.kind == "web_source").limit(500)
                )
            ).scalars()
            approved, rejected = set(), set()
            for fact in rows:
                meta = fact.metadata_ or {}
                domains = {d.lower().removeprefix("www.") for d in meta.get("domains") or [] if d}
                if meta.get("url"):
                    domains.add(_domain(str(meta["url"])))
                status = meta.get("source_status")
                if status == "approved":
                    approved |= domains
                elif status == "rejected":
                    rejected |= domains
            return approved, rejected
    except Exception as exc:  # noqa: BLE001
        logger.info("web_research_registry_unavailable", error=str(exc)[:200])
        return set(), set()


# ── the loop ─────────────────────────────────────────────────────────────────


class DeepLimits(BaseModel):
    max_rounds: int = Field(3, ge=1, le=4)
    max_pages: int = Field(12, ge=1, le=30)
    pages_per_round: int = Field(5, ge=1, le=10)
    deadline_s: float = Field(480, ge=30, le=600)


def _gap_queries(brief: ResearchBrief, missing: list[str], manufacturer: str | None) -> list[dict]:
    entity = " ".join(brief.entities[:2]) or brief.goal
    queries = []
    for name in missing[:3]:
        text = f"{entity} {name}"[:200]
        if query_leaks(text):
            continue
        if manufacturer:
            queries.append({"text": text, "language": "ru", "site": manufacturer})
        queries.append({"text": text, "language": "ru", "site": None})
    if missing and not query_leaks(entity):
        queries.append(
            {
                "text": f"{entity} паспорт технические характеристики pdf",
                "language": "ru",
                "site": None,
            }
        )
    return queries[:5]


async def deep_research(
    brief: ResearchBrief,
    limits: DeepLimits,
    *,
    fresh: bool = False,
    providers: list[str] | None = None,
) -> dict:
    """Rounds of search and reading until the brief is covered or the budget ends."""
    from app.ai import web_search_config as wsc
    from app.ai import web_urls
    from app.api import web_search as ws

    started = time.monotonic()
    deadline = started + limits.deadline_s
    approved, rejected = await registry_domains()
    preference = brief.source_preference or list(_DEFAULT_PREFERENCE)

    sources: list[dict] = []
    seen_urls: set[str] = set()
    fingerprints: list[tuple[int, int]] = []
    found: list[dict] = []
    follow: list[str] = []
    rounds: list[dict] = []
    diagnostics: list[str] = []
    llm_calls = 0
    queries = [q.model_dump() for q in brief.queries]
    gate = asyncio.Semaphore(max(1, wsc.get_config().research_concurrency))

    def missing_names() -> list[str]:
        have = {f["name"] for f in found}
        return [f.name for f in brief.facts_needed if f.name not in have]

    def single_source_names() -> list[str]:
        return [
            f.name
            for f in brief.facts_needed
            if len({x["domain"] for x in found if x["name"] == f.name}) == 1
        ]

    confirming = False

    for round_no in range(1, limits.max_rounds + 1):
        if time.monotonic() > deadline or len(sources) >= limits.max_pages:
            diagnostics.append("budget_exhausted")
            break
        missing = missing_names() if round_no > 1 else None

        async def _search(q: dict) -> list[str]:
            try:
                found_ = await ws.execute_web_search(
                    ws.WebSearchRequest(
                        query=q["text"],
                        limit=8,
                        domains=[q["site"]] if q.get("site") else None,
                        language=(q.get("language") or "ru")[:10],
                        providers=providers,
                        fresh=fresh,
                        intent="research",
                    )
                )
                return [r.url for r in found_.results]
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(f"search_error:{str(exc)[:80]}")
                return []

        lists = await asyncio.gather(*(_search(q) for q in queries))
        candidates = list(follow)
        for rank in range(max((len(x) for x in lists), default=0)):
            candidates.extend(x[rank] for x in lists if rank < len(x))
        fresh_candidates = []
        for url in web_urls.dedupe(candidates, cap=200):
            key = web_urls.url_key(url)
            if key in seen_urls or _domain(url) in rejected:
                continue
            fresh_candidates.append(url)
        # The owner's approved sources first, then the search order.
        fresh_candidates.sort(key=lambda u: _domain(u) not in approved)
        room = min(limits.pages_per_round, limits.max_pages - len(sources))
        picked = fresh_candidates[:room]
        follow = []
        if not picked:
            rounds.append({"round": round_no, "queries": [q["text"] for q in queries], "read": 0})
            diagnostics.append("no_new_sources")
            break

        async def _fetch(url: str):
            async with gate:
                try:
                    return url, await ws.fetch_page(
                        ws.WebFetchRequest(
                            url=url, max_chars=40000, include_links=True, fresh=fresh
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    return url, exc

        pages = await asyncio.gather(*(_fetch(u) for u in picked))
        facts_this_round = 0
        for url, page in pages:
            seen_urls.add(web_urls.url_key(url))
            n = len(sources) + 1
            entry: dict = {"n": n, "url": url, "domain": _domain(url), "round": round_no}
            sources.append(entry)
            if isinstance(page, Exception):
                entry.update(read_status="error", error=str(page)[:160])
                continue
            entry.update(
                title=page.title,
                read_status=page.read_status,
                challenge=page.challenge,
                source_ref=page.source_ref,
                text_chars=page.text_chars,
                trusted=entry["domain"] in approved,
            )
            if page.read_status != "ok":
                continue
            mark = web_urls.simhash(page.text)
            same = next(
                (m for m, other in fingerprints if web_urls.near_duplicate(mark, other)), None
            )
            if same is not None:
                entry["duplicate_of"] = same
                continue
            fingerprints.append((n, mark))
            if time.monotonic() > deadline:
                entry["skipped"] = "deadline"
                continue
            try:
                llm_calls += 1
                reading = await read_page(
                    brief,
                    page.text,
                    page.final_url or url,
                    page.title,
                    [link.model_dump() for link in page.links],
                    missing,
                )
            except ResearchModelError as exc:
                entry["reading_error"] = str(exc)[:160]
                diagnostics.append("model_error")
                continue
            kind = reading.kind if reading.kind in SOURCE_KINDS else "unknown"
            entry.update(
                kind=kind, kind_reason=reading.kind_reason[:200], about_entity=reading.about_entity
            )
            if not reading.about_entity:
                continue
            kept = dropped = 0
            for fact in reading.facts:
                problem = verify_fact(fact, page.text)
                if problem:
                    dropped += 1
                    diagnostics.append(f"fact_dropped:{problem}")
                    continue
                found.append(
                    {
                        "name": fact.name,
                        "value": fact.value[:120],
                        "unit": fact.unit,
                        "quote": fact.quote,
                        "n": n,
                        "domain": entry["domain"],
                    }
                )
                kept += 1
            entry.update(facts_found=kept, facts_dropped=dropped)
            facts_this_round += kept
            if kind in {"manufacturer", "official_distributor", "distributor", "reference"}:
                page_links = {link.url for link in page.links}
                follow.extend(p.url for p in reading.links[:3] if p.url in page_links)
        rounds.append(
            {
                "round": round_no,
                "queries": [q["text"] for q in queries],
                "read": len(pages),
                "facts": facts_this_round,
            }
        )
        still_missing = missing_names()
        manufacturer = next((s["domain"] for s in sources if s.get("kind") == "manufacturer"), None)
        if brief.facts_needed and not still_missing:
            # Everything has a value; a value only one site gives gets one
            # round to find a second site — then single it stays.
            single = single_source_names()
            if not single or confirming or round_no >= limits.max_rounds:
                break
            confirming = True
            queries = _gap_queries(brief, single, manufacturer)
            if not queries and not follow:
                break
            continue
        queries = _gap_queries(
            brief, still_missing or [f.name for f in brief.facts_needed], manufacturer
        )
        if not queries and not follow:
            break

    rank = {kind: i for i, kind in enumerate(preference)}
    sources.sort(
        key=lambda s: (
            s.get("read_status") != "ok",
            not s.get("trusted"),
            rank.get(s.get("kind") or "unknown", len(rank)),
            -(s.get("facts_found") or 0),
            s["n"],
        )
    )
    return {
        "brief": brief.model_dump(),
        "facts": aggregate_facts(brief, found),
        "sources": sources,
        "rounds": rounds,
        "stats": {
            "pages_read": sum(1 for s in sources if s.get("read_status") == "ok"),
            "pages_tried": len(sources),
            "model_calls": llm_calls,
            "elapsed_s": round(time.monotonic() - started, 1),
        },
        "diagnostics": diagnostics,
    }


_STATUS_LABEL = {
    "confirmed": "подтверждено",
    "single": "один источник",
    "conflict": "расхождение",
    "missing": "не найдено",
}


def deep_markdown(result: dict) -> str:
    brief = result["brief"]
    by_n = {s["n"]: s for s in result["sources"]}
    lines = [f"# {brief['goal']}", ""]
    lines += ["| Факт | Значение | Источники | Статус |", "|---|---|---|---|"]
    for row in result["facts"]:
        if not row["values"]:
            lines.append(f"| {row['name']} | — | — | {_STATUS_LABEL['missing']} |")
            continue
        for i, group in enumerate(row["values"]):
            value = f"{group['value']}" + (
                f" {group['unit']}"
                if group.get("unit") and group["unit"] not in group["value"]
                else ""
            )
            refs = ", ".join(f"[{n}]({by_n[n]['url']})" for n in group["sources"] if n in by_n)
            label = _STATUS_LABEL[row["status"]] if i == 0 else ""
            lines.append(f"| {row['name'] if i == 0 else ''} | {value} | {refs} | {label} |")
    lines += ["", "## Источники", ""]
    for s in sorted(result["sources"], key=lambda s: s["n"]):
        status = s.get("read_status")
        note = (
            f" — {s.get('kind', '')}" + (" · реестр" if s.get("trusted") else "")
            if status == "ok" and not s.get("duplicate_of")
            else f" — {('копия ' + str(s['duplicate_of'])) if s.get('duplicate_of') else status}"
        )
        lines.append(f"{s['n']}. [{s.get('title') or s['domain']}]({s['url']}){note}")
    stats = result["stats"]
    lines += [
        "",
        f"_Прочитано страниц: {stats['pages_read']} из {stats['pages_tried']}, "
        f"кругов поиска: {len(result['rounds'])}, {stats['elapsed_s']} с._",
    ]
    return "\n".join(lines)
