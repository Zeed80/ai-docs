"""Self-hosted web search + human-like page browsing for the agent.

The agent must not invent internet access by itself. This module is the single
backend boundary for the web:

* :func:`execute_web_search` turns a query into result URLs. The default engine
  is the self-hosted **SearXNG** metasearch service (no API key, no third-party
  cloud). Paid adapters (tavily/serper/brave/custom) remain available and can be
  used as a fallback — all configured from the GUI (:mod:`app.ai.web_search_config`).
* :func:`fetch_page` renders a URL through the **web-browser** sidecar (headless
  Chromium with stealth patches) so pages load like a human would see them —
  JavaScript executes, basic bot walls are cleared — and returns readable text
  plus an optional screenshot.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.ai import web_search_config as wsc
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


class WebSearchResult(BaseModel):
    title: str
    url: str
    snippet: str | None = None
    source: str | None = None
    published_at: str | None = None


class WebSearchResponse(BaseModel):
    query: str
    provider: str
    results: list[WebSearchResult]
    diagnostics: list[str] = []


def _recency_to_tavily_range(days: int | None) -> str | None:
    if not days:
        return None
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    if days <= 366:
        return "year"
    return None


def _recency_to_brave_freshness(days: int | None) -> str | None:
    if not days:
        return None
    if days <= 1:
        return "pd"
    if days <= 7:
        return "pw"
    if days <= 31:
        return "pm"
    if days <= 366:
        return "py"
    return None


def _recency_to_searxng_range(days: int | None) -> str | None:
    if not days:
        return None
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    return "year"


def _query_with_domains(query: str, domains: list[str] | None) -> str:
    clean_domains = [d.strip() for d in domains or [] if d and d.strip()]
    if not clean_domains:
        return query
    return f"{query} " + " OR ".join(f"site:{domain}" for domain in clean_domains)


def _build_provider_request(
    provider: str,
    endpoint: str,
    payload: WebSearchRequest,
    api_key: str | None,
    *,
    engines: list[str] | None = None,
) -> tuple[str, str, dict[str, Any] | None, dict[str, Any] | None, dict[str, str]]:
    if provider == "searxng":
        # SearXNG JSON API: GET /search?q=...&format=json. Self-hosted, keyless.
        params: dict[str, Any] = {
            "q": _query_with_domains(payload.query, payload.domains),
            "format": "json",
            "safesearch": 0,
        }
        time_range = _recency_to_searxng_range(payload.recency_days)
        if time_range:
            params["time_range"] = time_range
        if engines:
            params["engines"] = ",".join(engines)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return "GET", endpoint, params, None, headers

    if provider == "tavily":
        body: dict[str, Any] = {
            "query": payload.query,
            "max_results": payload.limit,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        time_range = _recency_to_tavily_range(payload.recency_days)
        if time_range:
            body["time_range"] = time_range
        if payload.domains:
            body["include_domains"] = payload.domains
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return "POST", endpoint, None, body, headers

    if provider == "serper":
        body = {
            "q": _query_with_domains(payload.query, payload.domains),
            "num": payload.limit,
        }
        if payload.recency_days:
            body["tbs"] = f"qdr:d{payload.recency_days}"
        headers = {"X-API-KEY": api_key} if api_key else {}
        return "POST", endpoint, None, body, headers

    if provider == "brave":
        params = {
            "q": _query_with_domains(payload.query, payload.domains),
            "count": payload.limit,
            "extra_snippets": "true",
        }
        freshness = _recency_to_brave_freshness(payload.recency_days)
        if freshness:
            params["freshness"] = freshness
        headers = {"X-Subscription-Token": api_key} if api_key else {}
        return "GET", endpoint, params, None, headers

    body = {
        "query": payload.query,
        "limit": payload.limit,
        "recency_days": payload.recency_days,
        "domains": payload.domains,
        "intent": payload.intent,
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return "POST", endpoint, None, body, headers


def _raw_results_for_provider(provider: str, data: Any) -> list[Any]:
    if not isinstance(data, dict):
        return data if isinstance(data, list) else []
    if provider == "searxng":
        return list(data.get("results") or [])
    if provider == "serper":
        return list(data.get("organic") or data.get("results") or [])
    if provider == "brave":
        web = data.get("web") if isinstance(data.get("web"), dict) else {}
        return list(web.get("results") or [])
    return list(data.get("results") or [])


def _snippet_for_item(provider: str, item: dict[str, Any]) -> str | None:
    snippet = item.get("snippet") or item.get("content") or item.get("description")
    if provider == "brave":
        extra = item.get("extra_snippets")
        if isinstance(extra, list) and extra:
            pieces = [str(snippet)] if snippet else []
            pieces.extend(str(part) for part in extra[:2] if part)
            snippet = " ".join(pieces)
    return str(snippet)[:1000] if snippet else None


def _normalize_results(
    provider: str,
    data: Any,
    *,
    limit: int,
) -> list[WebSearchResult]:
    results: list[WebSearchResult] = []
    for item in _raw_results_for_provider(provider, data)[:limit]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or "")
        if not url:
            continue
        source = item.get("source") or item.get("engine")
        if not source and provider == "brave":
            profile = item.get("profile")
            if isinstance(profile, dict):
                source = profile.get("name")
        published = (
            item.get("published_at")
            or item.get("date")
            or item.get("age")
            or item.get("publishedDate")
        )
        results.append(
            WebSearchResult(
                title=str(item.get("title") or item.get("name") or url)[:300],
                url=url,
                snippet=_snippet_for_item(provider, item),
                source=str(source or provider),
                published_at=str(published) if published else None,
            )
        )
    return results


async def _run_provider(
    provider: str,
    endpoint: str,
    payload: WebSearchRequest,
    api_key: str | None,
    *,
    engines: list[str] | None = None,
) -> list[WebSearchResult]:
    method, url, params, request_body, headers = _build_provider_request(
        provider, endpoint, payload, api_key, engines=engines
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        if method == "GET":
            response = await client.get(url, params=params, headers=headers)
        else:
            response = await client.post(url, json=request_body, headers=headers)
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "error_code": "web_search_provider_rejected",
                "message": response.text[:500],
            },
        )
    return _normalize_results(provider, response.json(), limit=payload.limit)


async def execute_web_search(payload: WebSearchRequest) -> WebSearchResponse:
    cfg = wsc.get_config()
    provider = cfg.provider
    if provider not in wsc.SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "web_search_provider_unsupported",
                "message": f"Unsupported web search provider: {provider}",
                "supported": list(wsc.SUPPORTED_PROVIDERS),
            },
        )
    endpoint = cfg.resolved_endpoint()
    if not endpoint:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "web_search_endpoint_missing",
                "message": "Web search endpoint is not configured.",
            },
        )

    diagnostics: list[str] = []
    try:
        results = await _run_provider(
            provider,
            endpoint,
            payload,
            cfg.api_key or None,
            engines=cfg.searxng_engines if provider == "searxng" else None,
        )
    except HTTPException:
        results = []
        diagnostics.append(f"{provider}_rejected")
    except httpx.HTTPError as exc:
        results = []
        diagnostics.append(f"{provider}_error:{str(exc)[:120]}")

    # Fallback to a secondary engine when the primary yields nothing.
    if not results and cfg.fallback_provider:
        fb_endpoint = cfg.resolved_fallback_endpoint()
        if fb_endpoint:
            try:
                results = await _run_provider(
                    cfg.fallback_provider,
                    fb_endpoint,
                    payload,
                    cfg.fallback_api_key or None,
                )
                if results:
                    provider = cfg.fallback_provider
                    diagnostics.append("used_fallback")
            except (HTTPException, httpx.HTTPError) as exc:
                diagnostics.append(f"fallback_error:{str(exc)[:120]}")

    if not results:
        diagnostics.append("provider_returned_no_results")
    return WebSearchResponse(
        query=payload.query,
        provider=provider,
        results=results,
        diagnostics=diagnostics,
    )


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


class WebFetchResponse(BaseModel):
    url: str
    final_url: str | None = None
    status: int | None = None
    title: str | None = None
    text: str = ""
    screenshot_b64: str | None = None
    truncated: bool = False
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
    base = cfg.browser_url.rstrip("/")
    body = {
        "url": payload.url,
        "screenshot": payload.screenshot,
        "max_chars": payload.max_chars,
        "wait_ms": payload.wait_ms,
        "pdf_ocr_pages": payload.ocr_max_pages if payload.ocr else 0,
    }
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

    return WebFetchResponse(
        url=payload.url,
        final_url=data.get("final_url"),
        status=data.get("status"),
        title=data.get("title"),
        text=text[: payload.max_chars],
        screenshot_b64=data.get("screenshot_b64"),
        truncated=bool(data.get("truncated")),
        diagnostics=diagnostics,
    )


@router.post("/fetch", response_model=WebFetchResponse)
async def fetch_web(payload: WebFetchRequest) -> WebFetchResponse:
    return await fetch_page(payload)


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
    per_page_chars: int = Field(6000, ge=500, le=60000)
    recency_days: int | None = Field(default=None, ge=1, le=3650)
    domains: list[str] | None = None
    intent: str | None = Field(default=None, max_length=120)
    # Also open URLs the agent already knows (e.g. a catalog PDF link).
    urls: list[str] | None = Field(default=None, max_length=12)
    # Publish a collated markdown report to the workspace ("рабочий стол").
    publish: bool = False
    canvas_id: str | None = None
    title: str | None = Field(default=None, max_length=200)


class WebResearchSource(BaseModel):
    url: str
    title: str | None = None
    snippet: str | None = None
    status: int | None = None
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


def _dedupe_urls(urls: list[str], cap: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        key = u.split("#", 1)[0].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
        if len(out) >= cap:
            break
    return out


def _research_markdown(query: str, sources: list[WebResearchSource]) -> str:
    lines = [f"# Веб-исследование: {query}", ""]
    lines.append(f"Прочитано источников: **{len(sources)}**")
    lines.append("")
    for i, s in enumerate(sources, 1):
        head = s.title or s.url
        tag = " · PDF" if s.is_pdf else ""
        lines.append(f"## {i}. {head}{tag}")
        lines.append(f"<{s.url}>")
        if s.snippet:
            lines.append("")
            lines.append(f"> {s.snippet}")
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
            detail={"error_code": "web_research_no_query", "message": "query, queries or urls required."},
        )

    diagnostics: list[str] = []
    provider = "searxng"
    candidate_urls: list[str] = list(payload.urls or [])
    snippet_by_url: dict[str, str] = {}

    # 1) Search each angle and pool the result URLs (the "nested" sweep).
    for angle in angles:
        try:
            found = await execute_web_search(
                WebSearchRequest(
                    query=angle,
                    limit=payload.search_limit,
                    recency_days=payload.recency_days,
                    domains=payload.domains,
                    intent=payload.intent or "research",
                )
            )
            provider = found.provider
            for r in found.results:
                candidate_urls.append(r.url)
                if r.snippet and r.url not in snippet_by_url:
                    snippet_by_url[r.url] = r.snippet
        except HTTPException:
            diagnostics.append(f"search_error:{angle[:40]}")

    urls = _dedupe_urls(candidate_urls, payload.max_sources)
    if not urls:
        return WebResearchResponse(
            query=angles[0] if angles else "",
            queries=angles,
            provider=provider,
            sources=[],
            diagnostics=diagnostics + ["no_urls_found"],
        )

    # 2) Read all chosen sources concurrently (HTML + PDF handled by sidecar).
    async def _read(u: str) -> WebResearchSource:
        try:
            page = await fetch_page(
                WebFetchRequest(url=u, screenshot=False, max_chars=payload.per_page_chars)
            )
            is_pdf = any("pdf" in d for d in page.diagnostics)
            return WebResearchSource(
                url=u,
                title=page.title,
                status=page.status,
                text=page.text,
                is_pdf=is_pdf,
                truncated=page.truncated,
                diagnostics=page.diagnostics,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            return WebResearchSource(url=u, diagnostics=[str(detail.get("error_code") or detail.get("message"))])
        except Exception as exc:  # noqa: BLE001
            return WebResearchSource(url=u, diagnostics=[f"read_error:{str(exc)[:80]}"])

    sources = list(await asyncio.gather(*(_read(u) for u in urls)))
    for s in sources:
        if s.url in snippet_by_url:
            s.snippet = snippet_by_url[s.url]

    primary_query = angles[0] if angles else "web research"

    # 3) Optionally publish a collated report to the workspace desktop.
    published_canvas_id: str | None = None
    if payload.publish:
        try:
            from app.core.chat_bus import chat_bus
            from app.domain.workspace import upsert_workspace_block

            canvas_id = payload.canvas_id or f"web-research-{abs(hash(primary_query)) % 10**8}"
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

    read_ok = sum(1 for s in sources if s.text)
    if not read_ok:
        diagnostics.append("no_sources_readable")
    return WebResearchResponse(
        query=primary_query,
        queries=angles,
        provider=provider,
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
@router.get("/settings", response_model=wsc.WebSearchConfigView)
async def get_settings() -> wsc.WebSearchConfigView:
    return wsc.to_view(wsc.get_config())


@router.patch("/settings", response_model=wsc.WebSearchConfigView)
async def patch_settings(patch: wsc.WebSearchConfigUpdate) -> wsc.WebSearchConfigView:
    return wsc.to_view(wsc.update_config(patch))


class WebSearchTestResponse(BaseModel):
    ok: bool
    provider: str
    result_count: int
    diagnostics: list[str] = []
    sample: list[WebSearchResult] = []


@router.post("/settings/test", response_model=WebSearchTestResponse)
async def test_settings() -> WebSearchTestResponse:
    """Run a live probe query against the currently configured search stack."""
    result = await execute_web_search(WebSearchRequest(query="test", limit=3))
    return WebSearchTestResponse(
        ok=bool(result.results),
        provider=result.provider,
        result_count=len(result.results),
        diagnostics=result.diagnostics,
        sample=result.results[:3],
    )
