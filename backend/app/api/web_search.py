"""Self-hosted web search + human-like page browsing for the agent.

The agent must not invent internet access by itself. This module is the single
backend boundary for the web:

* :func:`execute_web_search` turns a query into result URLs. It queries every
  enabled primary provider together and fuses their lists (RRF); fallback
  providers run only when the primaries found nothing. The default is one
  self-hosted **SearXNG** (no API key, no third-party cloud); adapters for
  paid APIs and MCP search servers live in :mod:`app.ai.web_providers`, the
  list in :mod:`app.ai.web_search_config`. Results are cached, paid
  providers are held to a daily quota counted before each call.
* :func:`fetch_page` renders a URL through the **web-browser** sidecar (headless
  Chromium with stealth patches) so pages load like a human would see them —
  JavaScript executes, basic bot walls are cleared — and returns readable text
  plus an optional screenshot. Page loads are paced per host across all
  callers (:func:`wait_for_host_slot`) and read pages are cached.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.ai import web_providers, web_urls
from app.ai import web_search_config as wsc
from app.ai.web_providers import WebSearchResult
from app.config import settings as _settings

router = APIRouter()


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
class WebSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(5, ge=1, le=20)
    recency_days: int | None = Field(default=None, ge=1, le=3650)
    domains: list[str] | None = None
    intent: str | None = Field(default=None, max_length=120)
    # Search language (ru, en, …); None → the configured default.
    language: str | None = Field(default=None, max_length=10)
    # Provider ids to query instead of the configured primaries.
    providers: list[str] | None = Field(default=None, max_length=10)
    # Skip the result cache (fresh news, a page just changed).
    fresh: bool = False


class WebSearchResponse(BaseModel):
    query: str
    provider: str
    results: list[WebSearchResult]
    # Provider ids that answered with results.
    providers_used: list[str] = []
    cached_at: str | None = None
    diagnostics: list[str] = []


# Kept for callers and tests that build one provider request by kind.
_dedupe_urls = web_urls.dedupe


def _legacy_spec(provider: str, endpoint: str | None, api_key: str | None, engines=None):
    return wsc.SearchProvider(
        id=provider, kind=provider, endpoint=endpoint, api_key=api_key or "", engines=engines or []
    )


def _build_provider_request(
    provider: str,
    endpoint: str,
    payload: WebSearchRequest,
    api_key: str | None,
    *,
    engines: list[str] | None = None,
):
    return web_providers.build_request(_legacy_spec(provider, endpoint, api_key, engines), payload)


def _normalize_results(provider: str, data: Any, *, limit: int) -> list[WebSearchResult]:
    return web_providers.normalize(provider, data, limit=limit)


async def _query_provider(spec: wsc.SearchProvider, q: WebSearchRequest) -> list[WebSearchResult]:
    return await web_providers.run(spec, q)


def _today() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


async def _take_quota(spec: wsc.SearchProvider) -> str | None:
    """None when the call may go; else the diagnostic why it may not.

    A call is counted before it is sent: a failed paid call may still be
    billed, and an unknown count is not zero — without Redis a quota'd
    provider is not called at all.
    """
    if spec.daily_quota is None:
        return None
    from app.utils.redis_client import get_async_redis

    key = f"webquota:{spec.id}:{_today()}"
    try:
        r = get_async_redis()
        used = await r.incr(key)
        if used == 1:
            await r.expire(key, 2 * 86400)
    except Exception:  # noqa: BLE001
        return f"quota_unknown:{spec.id}"
    if used > spec.daily_quota:
        return f"quota_exhausted:{spec.id}"
    return None


async def quota_used_today(ids: list[str]) -> dict[str, int]:
    from app.utils.redis_client import get_async_redis

    used: dict[str, int] = {}
    try:
        r = get_async_redis()
        for pid in ids:
            raw = await r.get(f"webquota:{pid}:{_today()}")
            used[pid] = int(raw or 0)
    except Exception:  # noqa: BLE001
        return {}
    return used


def _search_cache_key(specs: list[wsc.SearchProvider], q: WebSearchRequest) -> str:
    material = {
        "providers": [(s.id, s.kind, s.endpoint, s.engines, s.region) for s in specs],
        "q": q.model_dump(include={"query", "limit", "recency_days", "domains", "language"}),
    }
    digest = hashlib.sha1(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()
    return f"websearch:v1:{digest}"


async def _cache_get(key: str) -> dict | None:
    from app.utils.redis_client import get_async_redis

    try:
        raw = await get_async_redis().get(key)
    except Exception:  # noqa: BLE001
        return None
    return json.loads(raw) if raw else None


async def _cache_set(key: str, value: dict, ttl: int) -> None:
    if ttl <= 0:
        return
    from app.utils.redis_client import get_async_redis

    try:
        await get_async_redis().set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
    except Exception:  # noqa: BLE001
        pass


def _usable(spec: wsc.SearchProvider) -> str | None:
    """None when the provider can be queried; else the error code."""
    if spec.kind not in wsc.SUPPORTED_PROVIDERS:
        return "web_search_provider_unsupported"
    if spec.kind == "mcp":
        return None if spec.mcp_tool else "web_search_endpoint_missing"
    if not spec.resolved_endpoint():
        return "web_search_endpoint_missing"
    return None


_UNUSABLE_MESSAGES = {
    "web_search_provider_unsupported": "Unsupported web search provider",
    "web_search_endpoint_missing": "Web search endpoint is not configured.",
}


async def _run_group(
    specs: list[wsc.SearchProvider], q: WebSearchRequest, diagnostics: list[str]
) -> tuple[list[WebSearchResult], list[str]]:
    """Query providers together; a failure is a diagnostic, never a silent swap."""

    async def _one(spec: wsc.SearchProvider) -> list[WebSearchResult] | None:
        refused = await _take_quota(spec)
        if refused:
            diagnostics.append(refused)
            return None
        try:
            return await _query_provider(spec, q)
        except web_providers.ProviderRejected as exc:
            diagnostics.append(f"{spec.id}_rejected:{str(exc)[:120]}")
        except HTTPException:
            diagnostics.append(f"{spec.id}_rejected")
        except (httpx.HTTPError, ValueError) as exc:
            diagnostics.append(f"{spec.id}_error:{str(exc)[:120]}")
        return None

    answers = await asyncio.gather(*(_one(spec) for spec in specs))
    ranked = [
        (spec.id, spec.weight, found) for spec, found in zip(specs, answers, strict=True) if found
    ]
    if len(ranked) == 1:
        return ranked[0][2][: q.limit], [ranked[0][0]]
    return web_providers.fuse(ranked, q.limit), [pid for pid, _, _ in ranked]


async def execute_web_search(payload: WebSearchRequest) -> WebSearchResponse:
    cfg = wsc.get_config()
    if payload.language is None:
        payload = payload.model_copy(update={"language": cfg.language or None})
    providers = [p for p in cfg.effective_providers() if p.enabled]
    if payload.providers:
        wanted = {pid.strip().lower() for pid in payload.providers}
        primaries = [p for p in providers if p.id in wanted]
        fallbacks: list[wsc.SearchProvider] = []
        missing = wanted - {p.id for p in primaries}
        if missing:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "web_search_provider_unknown",
                    "message": f"Нет включённых провайдеров: {', '.join(sorted(missing))}",
                    "available": [p.id for p in providers],
                },
            )
    else:
        primaries = [p for p in providers if p.role == "primary"]
        fallbacks = [p for p in providers if p.role == "fallback"]

    diagnostics: list[str] = []
    usable = []
    for spec in primaries:
        problem = _usable(spec)
        if problem:
            diagnostics.append(f"{spec.id}:{problem}")
        else:
            usable.append(spec)
    if not usable:
        code = (
            "web_search_provider_unsupported"
            if any(_usable(s) == "web_search_provider_unsupported" for s in primaries)
            else "web_search_endpoint_missing"
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": code,
                "message": _UNUSABLE_MESSAGES[code],
                "supported": list(wsc.SUPPORTED_PROVIDERS),
            },
        )

    cache_key = _search_cache_key(usable + fallbacks, payload)
    if not payload.fresh:
        cached = await _cache_get(cache_key)
        if cached:
            response = WebSearchResponse(**cached)
            response.diagnostics = [*response.diagnostics, "cached"]
            return response

    results, used = await _run_group(usable, payload, diagnostics)
    usable_fallbacks = [s for s in fallbacks if not _usable(s)]
    if not results and usable_fallbacks:
        results, used = await _run_group(usable_fallbacks, payload, diagnostics)
        if results:
            diagnostics.append("used_fallback")

    if not results:
        diagnostics.append("provider_returned_no_results")
    response = WebSearchResponse(
        query=payload.query,
        provider="+".join(used) or usable[0].id,
        results=results,
        providers_used=used,
        diagnostics=diagnostics,
    )
    if results:
        stamped = response.model_copy(update={"cached_at": datetime.now(UTC).isoformat()})
        await _cache_set(cache_key, stamped.model_dump(), cfg.search_cache_ttl_s)
    return response


@router.post("/query", response_model=WebSearchResponse)
async def query_web(payload: WebSearchRequest) -> WebSearchResponse:
    return await execute_web_search(payload)


# --------------------------------------------------------------------------- #
# Human-like page fetch (browser sidecar)
# --------------------------------------------------------------------------- #
class WebFetchRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2048)
    # Return a screenshot (base64 PNG) of the rendered page in addition to text.
    screenshot: bool = False
    # Max characters of extracted text to return.
    max_chars: int = Field(20000, ge=500, le=200000)
    # Extra time (ms) to wait for late client-side content.
    wait_ms: int = Field(0, ge=0, le=15000)
    # OCR scanned PDFs (no text layer) via the local VLM. Cap pages to bound cost.
    ocr: bool = True
    ocr_max_pages: int = Field(5, ge=0, le=20)
    # Also return the page's outgoing links — a catalog page advertises its PDFs
    # through <a href>, not through the readable text the extractor returns.
    include_links: bool = False
    # What the agent is looking for on the page: the answer leads with the
    # passages about it, the full text stays readable through /source.
    query: str | None = Field(default=None, max_length=500)
    # Load the page again instead of the cached copy.
    fresh: bool = False


class PageLink(BaseModel):
    url: str
    text: str = ""


class Passage(BaseModel):
    offset: int
    text: str


class WebFetchResponse(BaseModel):
    url: str
    final_url: str | None = None
    status: int | None = None
    title: str | None = None
    # ok | blocked | empty | error — a bot wall is not a page that was read.
    read_status: str = "ok"
    # Kind of the bot wall that stayed (cloudflare, ddos_guard, qrator,
    # yandex_smartcaptcha, …): a desktop session + handoff gets past it.
    challenge: str | None = None
    text_chars: int = 0
    # Full text for read_source; fields before ``text`` survive the trim of a
    # long page in the model's history.
    source_ref: str | None = None
    passages: list[Passage] = []
    text: str = ""
    links: list[PageLink] = []
    screenshot_b64: str | None = None
    truncated: bool = False
    cached_at: str | None = None
    diagnostics: list[str] = []


_OCR_PROMPT = (
    "Это страница отсканированного PDF (прайс-лист/каталог/даташит). "
    "Извлеки ВЕСЬ текст дословно, сохраняя строки и числа (цены, артикулы, "
    "размеры). Таблицы передавай построчно. Не добавляй пояснений."
)


async def _ocr_pdf_pages(images_b64: list[str], max_chars: int) -> str:
    """OCR scanned-PDF page images with the local VLM (gemma4:e4b by default)."""
    import base64

    from app.ai.ollama_client import chat_with_images

    model = getattr(_settings, "ollama_model_ocr", None)
    parts: list[str] = []
    for b64 in images_b64:
        try:
            img = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            continue
        try:
            resp = await chat_with_images(
                _OCR_PROMPT, [img], model=model, temperature=0.0, max_tokens=4096
            )
            page_text = (getattr(resp, "text", "") or "").strip()
        except Exception:  # noqa: BLE001
            page_text = ""
        if page_text:
            parts.append(page_text)
        if sum(len(p) for p in parts) >= max_chars:
            break
    return "\n\n".join(parts).strip()[:max_chars]


_TERM = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:[.,][0-9]+)?")
# Prefix length that folds Russian endings ("насоса", "насосы" → "насос").
_STEM = 5


def _query_terms(query: str | None) -> set[str]:
    terms: set[str] = set()
    for word in _TERM.findall(query or ""):
        word = word.lower().replace("ё", "е")
        if word[0].isdigit():
            terms.add(word.replace(",", "."))
        elif len(word) >= 3:
            terms.add(word[:_STEM])
    return terms


def _window_score(window: str, terms: set[str]) -> int:
    hits: set[str] = set()
    for word in _TERM.findall(window):
        word = word.lower().replace("ё", "е")
        key = word.replace(",", ".") if word[0].isdigit() else word[:_STEM]
        if key in terms:
            hits.add(key)
    return len(hits)


def extract_passages(text: str, query: str | None, budget: int, max_n: int = 3) -> list[Passage]:
    """Passages of ``text`` that mention the query terms, in page order.

    The model's history holds a few thousand characters per call, so a long
    page is shown as the places that answer the question; the rest is read
    on request. Without matching terms the page opening is the passage.
    """
    text = text or ""
    if not text.strip() or budget <= 0:
        return []
    size = max(200, budget // max(1, max_n))
    terms = _query_terms(query)
    step = max(1, size // 2)
    scored: list[tuple[int, int]] = []
    if terms:
        for start in range(0, max(1, len(text) - size // 2), step):
            score = _window_score(text[start : start + size], terms)
            if score:
                scored.append((score, start))
    picked: list[int] = []
    for _, start in sorted(scored, key=lambda item: (-item[0], item[1])):
        if all(abs(start - other) >= size for other in picked):
            picked.append(start)
        if len(picked) >= max_n:
            break
    if not picked:
        picked = [0]
    return [
        Passage(offset=start, text=text[start : start + size].strip()) for start in sorted(picked)
    ]


def classify_read(status: int | None, text: str, diagnostics: list[str]) -> str:
    if any(d.startswith("bot_challenge") for d in diagnostics):
        return "blocked"
    if status is not None and status >= 400:
        return "error"
    if not (text or "").strip():
        return "empty"
    return "ok"


# Full page text the agent can read on in later calls of the same work.
SOURCE_TTL = 6 * 3600
_SOURCE_PREFIX = "websrc:v1:"


async def store_source(url: str, title: str | None, text: str) -> str | None:
    """Keep the full text owner-scoped; None when Redis is unavailable."""
    if not text:
        return None
    from app.domain.workspace import _owner
    from app.utils.redis_client import get_async_redis

    ref = f"{_SOURCE_PREFIX}{uuid.uuid4().hex}"
    try:
        await get_async_redis().set(
            ref,
            json.dumps(
                {"owner_key": _owner(), "url": url, "title": title, "text": text},
                ensure_ascii=False,
            ),
            ex=SOURCE_TTL,
        )
    except Exception:  # noqa: BLE001
        return None
    return ref


async def load_source(ref: str) -> dict | None:
    from app.domain.workspace import _owner
    from app.utils.redis_client import get_async_redis

    if not ref.startswith(_SOURCE_PREFIX) or len(ref) != len(_SOURCE_PREFIX) + 32:
        return None
    raw = await get_async_redis().get(ref)
    if not raw:
        return None
    stored = json.loads(raw)
    if not isinstance(stored, dict) or stored.get("owner_key") != _owner():
        return None
    return stored


# One host is loaded no more often than once per interval (plus jitter) by
# every caller together: research, browse, catalog crawl, discovery. Without
# it twelve parallel reads of one supplier site looked like a scraper.
_HOST_SLOT_LUA = """
local now = tonumber(ARGV[1])
local step = tonumber(ARGV[2])
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = math.max(now, nxt)
redis.call('SET', KEYS[1], tostring(slot + step), 'PX', math.ceil((slot + step - now) * 1000) + 1000)
return tostring(slot - now)
"""
_local_slots: dict[str, float] = {}
# A wait longer than this is not a pace but a queue: go anyway.
MAX_HOST_WAIT_S = 60.0


async def wait_for_host_slot(url: str, interval: float) -> float:
    """Sleep until this host may be loaded again; returns the seconds waited."""
    if interval <= 0:
        return 0.0
    host = web_urls.url_key(url).split("/", 1)[0]
    step = interval * random.uniform(1.0, 1.6)
    now = time.time()
    try:
        from app.utils.redis_client import get_async_redis

        wait = float(await get_async_redis().eval(_HOST_SLOT_LUA, 1, f"webrate:{host}", now, step))
    except Exception:  # noqa: BLE001 — no Redis/Lua: pace within this process
        slot = max(now, _local_slots.get(host, 0.0))
        _local_slots[host] = slot + step
        wait = slot - now
    wait = min(max(0.0, wait), MAX_HOST_WAIT_S)
    if wait:
        await asyncio.sleep(wait)
    return wait


def profile_owner() -> str:
    """Whose kept browser profile a read uses: the person the work is for.

    Background jobs with no acting user (catalog crawl, reflection) share the
    ``system`` profile; nobody signs in there.
    """
    from app.ai.actor_context import get_acting_user

    return get_acting_user() or "system"


def browser_profile_fields(
    owner: str, profile: str = "reader", cfg: wsc.WebSearchConfig | None = None
) -> dict:
    """Profile, exit and persona the browser service is asked to use.

    A profile keeps one proxy for good — chosen by a hash of the profile, so
    it survives restarts without a table — and takes that proxy's language
    and timezone: a returning visitor does not jump countries between visits.
    """
    cfg = cfg or wsc.get_config()
    persona = {"locale": cfg.browser_locale, "timezone": cfg.browser_timezone}
    fields: dict = {"profile_owner": owner, "profile": profile, "persona": persona}
    pool = sorted((p for p in cfg.proxies if p.enabled), key=lambda p: p.id)
    if pool:
        digest = hashlib.sha256(f"{owner}|{profile}".encode()).digest()
        chosen = pool[int.from_bytes(digest[:8], "big") % len(pool)]
        fields["proxy"] = {
            "id": chosen.id,
            "server": chosen.server,
            "username": chosen.username or None,
            "password": chosen.password or None,
        }
        persona.update(
            {k: v for k, v in (("locale", chosen.locale), ("timezone", chosen.timezone)) if v}
        )
    return fields


def _page_cache_key(payload: WebFetchRequest, owner: str) -> str:
    material = "|".join(
        str(part)
        for part in (
            # A page read in someone's profile may show what only they see.
            owner,
            web_urls.url_key(payload.url),
            payload.max_chars,
            payload.include_links,
            payload.ocr,
            payload.ocr_max_pages,
            payload.wait_ms,
        )
    )
    return f"webpage:v1:{hashlib.sha1(material.encode()).hexdigest()}"


async def _fetch_live(
    payload: WebFetchRequest, cfg: wsc.WebSearchConfig, owner: str
) -> WebFetchResponse:
    base = cfg.browser_url.rstrip("/")
    body = {
        **browser_profile_fields(owner, cfg=cfg),
        "url": payload.url,
        "screenshot": payload.screenshot,
        "max_chars": payload.max_chars,
        "wait_ms": payload.wait_ms,
        "pdf_ocr_pages": payload.ocr_max_pages if payload.ocr else 0,
        "include_links": payload.include_links,
    }
    await wait_for_host_slot(payload.url, cfg.per_host_interval_s)
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(f"{base}/fetch", json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "web_browser_unreachable", "message": str(exc)},
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "error_code": "web_browser_error",
                "message": response.text[:500],
            },
        )
    data = response.json()
    text = str(data.get("text") or "")
    diagnostics = list(data.get("diagnostics") or [])

    # Scanned PDF with no text layer → OCR the rasterized pages locally.
    page_images = data.get("page_images_b64") or []
    if payload.ocr and not text and page_images:
        ocr_text = await _ocr_pdf_pages(page_images, payload.max_chars)
        if ocr_text:
            text = ocr_text
            diagnostics.append(f"ocr_applied:{len(page_images)}")
        else:
            diagnostics.append("ocr_empty")

    links = [
        PageLink(url=str(item.get("url") or ""), text=str(item.get("text") or ""))
        for item in (data.get("links") or [])
        if isinstance(item, dict) and item.get("url")
    ]
    return WebFetchResponse(
        url=payload.url,
        final_url=data.get("final_url"),
        status=data.get("status"),
        title=data.get("title"),
        read_status=classify_read(data.get("status"), text, diagnostics),
        challenge=next(
            (d.split(":", 1)[1] for d in diagnostics if d.startswith("challenge:")), None
        ),
        text_chars=len(text),
        text=text[: payload.max_chars],
        links=links,
        screenshot_b64=data.get("screenshot_b64"),
        truncated=bool(data.get("truncated")),
        diagnostics=diagnostics,
    )


async def fetch_page(payload: WebFetchRequest) -> WebFetchResponse:
    cfg = wsc.get_config()
    if not cfg.browsing_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "web_browsing_disabled",
                "message": "Human-like page browsing is disabled in settings.",
            },
        )
    # A screenshot shows the page now; only text reads are cached.
    cacheable = not payload.screenshot
    owner = profile_owner()
    cache_key = _page_cache_key(payload, owner) if cacheable else ""
    page = None
    if cacheable and not payload.fresh:
        cached = await _cache_get(cache_key)
        if cached:
            page = WebFetchResponse(**cached)
            page.diagnostics = [*page.diagnostics, "cached"]
    if page is None:
        page = await _fetch_live(payload, cfg, owner)
        # Only a page that was read is kept: a bot wall must be retried.
        if cacheable and page.read_status == "ok":
            stamped = page.model_copy(update={"cached_at": datetime.now(UTC).isoformat()})
            await _cache_set(
                cache_key,
                stamped.model_dump(exclude={"source_ref", "passages", "screenshot_b64"}),
                cfg.page_cache_ttl_s,
            )
    if page.text:
        page.source_ref = await store_source(payload.url, page.title, page.text)
    page.passages = extract_passages(page.text, payload.query, 1800) if payload.query else []
    return page


@router.post("/fetch", response_model=WebFetchResponse)
async def fetch_web(payload: WebFetchRequest) -> WebFetchResponse:
    return await fetch_page(payload)


class ReadSourceRequest(BaseModel):
    source_ref: str = Field(..., min_length=10, max_length=80)
    # With a query: the passages about it; otherwise text from ``offset``.
    query: str | None = Field(default=None, max_length=500)
    offset: int = Field(0, ge=0)
    max_chars: int = Field(4000, ge=200, le=4000)


class ReadSourceResponse(BaseModel):
    source_ref: str
    url: str
    title: str | None = None
    text_chars: int
    offset: int
    next_offset: int | None = None
    passages: list[Passage] = []
    text: str = ""


@router.post("/source", response_model=ReadSourceResponse)
async def read_source(payload: ReadSourceRequest) -> ReadSourceResponse:
    """Read on in a page already fetched this work, without fetching it again."""
    stored = await load_source(payload.source_ref)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "web_source_expired",
                "message": "Источник не найден или устарел — открой страницу заново (browse).",
            },
        )
    text = str(stored.get("text") or "")
    base = {
        "source_ref": payload.source_ref,
        "url": str(stored.get("url") or ""),
        "title": stored.get("title"),
        "text_chars": len(text),
    }
    if payload.query:
        return ReadSourceResponse(
            **base,
            offset=0,
            passages=extract_passages(text, payload.query, payload.max_chars, max_n=4),
        )
    end = payload.offset + payload.max_chars
    return ReadSourceResponse(
        **base,
        offset=payload.offset,
        next_offset=end if end < len(text) else None,
        text=text[payload.offset : end],
    )


# --------------------------------------------------------------------------- #
# Deep research: search several angles, read many sources (incl. PDF), collate
# --------------------------------------------------------------------------- #
class WebResearchRequest(BaseModel):
    # One focused query, or several angles for a broader ("nested") sweep.
    query: str | None = Field(default=None, max_length=500)
    queries: list[str] | None = Field(default=None, max_length=8)
    # How many distinct sources to actually open and read.
    max_sources: int = Field(5, ge=1, le=12)
    # Result URLs to pull from search before de-duplicating to max_sources.
    search_limit: int = Field(8, ge=1, le=20)
    # Passages are picked on the server, so the whole page is read: at 6000
    # the value further down a datasheet was never seen.
    per_page_chars: int = Field(40000, ge=500, le=60000)
    recency_days: int | None = Field(default=None, ge=1, le=3650)
    domains: list[str] | None = None
    intent: str | None = Field(default=None, max_length=120)
    # Also open URLs the agent already knows (e.g. a catalog PDF link).
    urls: list[str] | None = Field(default=None, max_length=12)
    # Publish a collated markdown report to the workspace ("рабочий стол").
    publish: bool = False
    canvas_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    # Full page texts in the answer. The agent gets passages and a source_ref
    # instead: five full pages never fit its history and it saw only the first.
    include_text: bool = False
    language: str | None = Field(default=None, max_length=10)
    providers: list[str] | None = Field(default=None, max_length=10)
    fresh: bool = False


# Characters of passages for all sources together — the whole answer has to
# stay under the history budget (agent_loop._MAX_TOOL_RESULT_CHARS).
RESEARCH_PASSAGE_BUDGET = 3200


class WebResearchSource(BaseModel):
    n: int = 0
    url: str
    title: str | None = None
    snippet: str | None = None
    status: int | None = None
    read_status: str = "ok"
    text_chars: int = 0
    source_ref: str | None = None
    passages: list[Passage] = []
    # n of an earlier source with the same text (mirror, print version).
    duplicate_of: int | None = None
    cached: bool = False
    text: str = ""
    is_pdf: bool = False
    truncated: bool = False
    diagnostics: list[str] = []


class WebResearchResponse(BaseModel):
    query: str
    queries: list[str]
    provider: str
    sources: list[WebResearchSource]
    published_canvas_id: str | None = None
    diagnostics: list[str] = []


def _research_markdown(query: str, sources: list[WebResearchSource]) -> str:
    lines = [f"# Веб-исследование: {query}", ""]
    read_ok = sum(1 for s in sources if s.read_status == "ok")
    lines.append(f"Прочитано источников: **{read_ok}** из {len(sources)}")
    lines.append("")
    for i, s in enumerate(sources, 1):
        head = s.title or s.url
        tag = " · PDF" if s.is_pdf else ""
        lines.append(f"## {i}. {head}{tag}")
        lines.append(f"<{s.url}>")
        if s.snippet:
            lines.append("")
            lines.append(f"> {s.snippet}")
        if s.read_status == "blocked":
            lines.append("")
            lines.append("_Сайт показал проверку на бота — страница не прочитана._")
            lines.append("")
            continue
        if s.duplicate_of:
            lines.append("")
            lines.append(f"_Тот же текст, что в источнике {s.duplicate_of}._")
            lines.append("")
            continue
        excerpt = (s.text or "").strip()
        if excerpt:
            excerpt = excerpt[:1200] + ("…" if len(excerpt) > 1200 else "")
            lines.append("")
            lines.append(excerpt)
        if not excerpt and s.diagnostics:
            lines.append("")
            lines.append(f"_Не удалось прочитать: {', '.join(s.diagnostics)}_")
        lines.append("")
    return "\n".join(lines)


async def execute_web_research(payload: WebResearchRequest) -> WebResearchResponse:
    angles = [q.strip() for q in (payload.queries or []) if q and q.strip()]
    if payload.query and payload.query.strip():
        angles.insert(0, payload.query.strip())
    if not angles and not payload.urls:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "web_research_no_query",
                "message": "query, queries or urls required.",
            },
        )

    diagnostics: list[str] = []
    provider: str | None = None
    candidate_urls: list[str] = list(payload.urls or [])
    snippet_by_url: dict[str, str] = {}

    # 1) Search every angle at once and pool the result URLs in angle order.
    async def _search(angle: str) -> WebSearchResponse | None:
        try:
            return await execute_web_search(
                WebSearchRequest(
                    query=angle,
                    limit=payload.search_limit,
                    recency_days=payload.recency_days,
                    domains=payload.domains,
                    intent=payload.intent or "research",
                    language=payload.language,
                    providers=payload.providers,
                    fresh=payload.fresh,
                )
            )
        except HTTPException:
            diagnostics.append(f"search_error:{angle[:40]}")
            return None

    for found in await asyncio.gather(*(_search(angle) for angle in angles)):
        if found is None:
            continue
        provider = provider or found.provider
        diagnostics.extend(d for d in found.diagnostics if d not in diagnostics)
        for r in found.results:
            candidate_urls.append(r.url)
            if r.snippet and r.url not in snippet_by_url:
                snippet_by_url[r.url] = r.snippet

    urls = _dedupe_urls(candidate_urls, payload.max_sources)
    if not urls:
        return WebResearchResponse(
            query=angles[0] if angles else "",
            queries=angles,
            provider=provider or "none",
            sources=[],
            diagnostics=diagnostics + ["no_urls_found"],
        )

    # 2) Read the chosen sources, a few at a time (HTML + PDF by the sidecar).
    gate = asyncio.Semaphore(max(1, wsc.get_config().research_concurrency))

    async def _read(u: str) -> WebResearchSource:
        try:
            async with gate:
                page = await fetch_page(
                    WebFetchRequest(
                        url=u,
                        screenshot=False,
                        max_chars=payload.per_page_chars,
                        fresh=payload.fresh,
                    )
                )
            is_pdf = any("pdf" in d for d in page.diagnostics)
            return WebResearchSource(
                url=u,
                title=page.title,
                status=page.status,
                read_status=page.read_status,
                text_chars=page.text_chars,
                source_ref=page.source_ref,
                cached="cached" in page.diagnostics,
                text=page.text,
                is_pdf=is_pdf,
                truncated=page.truncated,
                diagnostics=page.diagnostics,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            return WebResearchSource(
                url=u,
                read_status="error",
                diagnostics=[str(detail.get("error_code") or detail.get("message"))],
            )
        except Exception as exc:  # noqa: BLE001
            return WebResearchSource(
                url=u, read_status="error", diagnostics=[f"read_error:{str(exc)[:80]}"]
            )

    sources = list(await asyncio.gather(*(_read(u) for u in urls)))
    primary_query = angles[0] if angles else "web research"
    fingerprints: list[tuple[int, int]] = []
    for n, s in enumerate(sources, 1):
        if s.read_status != "ok":
            continue
        mark = web_urls.simhash(s.text)
        same = next((m for m, other in fingerprints if web_urls.near_duplicate(mark, other)), None)
        if same is not None:
            s.duplicate_of = same
        else:
            fingerprints.append((n, mark))
    readable = [s for s in sources if s.read_status == "ok" and s.duplicate_of is None]
    per_source = RESEARCH_PASSAGE_BUDGET // max(1, len(readable))
    passage_query = " ".join(angles) or payload.intent
    for n, s in enumerate(sources, 1):
        s.n = n
        if s.url in snippet_by_url:
            s.snippet = snippet_by_url[s.url][:240]
        if s.read_status == "ok" and s.duplicate_of is None:
            s.passages = extract_passages(s.text, passage_query, per_source)

    # 3) Optionally publish a collated report to the workspace desktop.
    published_canvas_id: str | None = None
    if payload.publish:
        try:
            from app.core.chat_bus import chat_bus
            from app.domain.workspace import upsert_workspace_block

            # hash() is salted per process: the same query got a new block
            # after every restart instead of updating its report.
            digest = hashlib.sha1(primary_query.encode("utf-8")).hexdigest()[:10]
            canvas_id = payload.canvas_id or f"web-research-{digest}"
            block = {
                "id": canvas_id,
                "type": "markdown",
                "title": payload.title or f"Веб-исследование: {primary_query}"[:200],
                "content": _research_markdown(primary_query, sources),
                "source": "web_research",
            }
            stored = upsert_workspace_block(canvas_id, block)
            await chat_bus.publish(
                {"type": "workspace.updated", "canvas_id": canvas_id, "block": stored}
            )
            published_canvas_id = canvas_id
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"publish_error:{str(exc)[:120]}")

    if not readable:
        diagnostics.append("no_sources_readable")
    if not payload.include_text:
        for s in sources:
            s.text = ""
    return WebResearchResponse(
        query=primary_query,
        queries=angles,
        provider=provider or "none",
        sources=sources,
        published_canvas_id=published_canvas_id,
        diagnostics=diagnostics,
    )


@router.post("/research", response_model=WebResearchResponse)
async def research_web(payload: WebResearchRequest) -> WebResearchResponse:
    return await execute_web_research(payload)


# --------------------------------------------------------------------------- #
# Settings (GUI)
# --------------------------------------------------------------------------- #
async def _view(cfg: wsc.WebSearchConfig) -> wsc.WebSearchConfigView:
    quota_ids = [p.id for p in cfg.effective_providers() if p.daily_quota is not None]
    return wsc.to_view(cfg, await quota_used_today(quota_ids) if quota_ids else {})


@router.get("/settings", response_model=wsc.WebSearchConfigView)
async def get_settings() -> wsc.WebSearchConfigView:
    return await _view(wsc.get_config())


@router.patch("/settings", response_model=wsc.WebSearchConfigView)
async def patch_settings(patch: wsc.WebSearchConfigUpdate) -> wsc.WebSearchConfigView:
    try:
        cfg = wsc.update_config(patch)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "web_search_settings_invalid", "message": str(exc)},
        ) from exc
    return await _view(cfg)


class WebSearchTestResponse(BaseModel):
    ok: bool
    provider: str
    result_count: int
    diagnostics: list[str] = []
    sample: list[WebSearchResult] = []


class WebSearchTestRequest(BaseModel):
    # One provider id; None — the configured stack as the agent uses it.
    provider_id: str | None = None
    query: str = Field("промышленный насос характеристики", min_length=1, max_length=200)


@router.post("/settings/test", response_model=WebSearchTestResponse)
async def test_settings(body: WebSearchTestRequest | None = None) -> WebSearchTestResponse:
    """Run a live probe query against one provider or the whole search stack."""
    body = body or WebSearchTestRequest()
    result = await execute_web_search(
        WebSearchRequest(
            query=body.query,
            limit=3,
            fresh=True,
            providers=[body.provider_id] if body.provider_id else None,
        )
    )
    return WebSearchTestResponse(
        ok=bool(result.results),
        provider=result.provider,
        result_count=len(result.results),
        diagnostics=result.diagnostics,
        sample=result.results[:3],
    )


class CacheClearResponse(BaseModel):
    removed: int


@router.post("/settings/cache/clear", response_model=CacheClearResponse)
async def clear_cache() -> CacheClearResponse:
    """Drop cached search results and pages (not the agent's open sources)."""
    from app.utils.redis_client import get_async_redis

    r = get_async_redis()
    removed = 0
    for pattern in ("websearch:v1:*", "webpage:v1:*"):
        batch = [key async for key in r.scan_iter(match=pattern, count=500)]
        if batch:
            removed += await r.delete(*batch)
    return CacheClearResponse(removed=removed)


# --------------------------------------------------------------------------- #
# W2: the person's kept browser profiles and the fingerprint self-check
# --------------------------------------------------------------------------- #
async def _browser_call(path: str, body: dict, timeout: float = 30.0) -> dict:
    cfg = wsc.get_config()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(cfg.browser_url.rstrip("/") + path, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail={"error_code": "web_browser_unreachable", "message": str(exc)}
        ) from exc
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "web_browser_error", "message": response.text[:300]},
        )
    return response.json()


@router.get("/browser/profiles")
async def list_browser_profiles() -> dict:
    """The current person's kept profiles: size, last use, bound proxy."""
    return await _browser_call("/profiles/list", {"owner": profile_owner()})


@router.delete("/browser/profiles/{name}")
async def delete_browser_profile(name: str) -> dict:
    """Forget a profile — cookies, logins, passed bot checks — of the current person."""
    answer = await _browser_call("/profiles/delete", {"owner": profile_owner(), "name": name})
    if not answer.get("ok"):
        raise HTTPException(
            status_code=409,
            detail={"error_code": answer.get("error"), "message": "Профиль сейчас используется"},
        )
    return answer


class SelftestRequest(BaseModel):
    url: str = Field("https://bot.sannysoft.com/", max_length=500)


@router.post("/browser/selftest")
async def browser_selftest(body: SelftestRequest | None = None) -> dict:
    """What a site's bot detector sees: in the current person's reading
    profile, through the proxy that profile is bound to."""
    body = body or SelftestRequest()
    return await _browser_call(
        "/selftest", {"url": body.url, **browser_profile_fields(profile_owner())}, timeout=90.0
    )
