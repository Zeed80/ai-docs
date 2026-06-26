"""Controlled web-search adapter for the agent.

The agent must not invent internet access by itself. This endpoint is the single
backend boundary for external search providers; deployments enable it explicitly
with WEB_SEARCH_PROVIDER and provider credentials.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

_SUPPORTED_PROVIDERS = {"tavily", "serper", "brave", "custom"}
_DEFAULT_ENDPOINTS = {
    "tavily": "https://api.tavily.com/search",
    "serper": "https://google.serper.dev/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
}


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


def _query_with_domains(query: str, domains: list[str] | None) -> str:
    clean_domains = [d.strip() for d in domains or [] if d and d.strip()]
    if not clean_domains:
        return query
    return f"{query} " + " OR ".join(f"site:{domain}" for domain in clean_domains)


def _provider_config() -> tuple[str, str | None, str | None]:
    provider = os.getenv("WEB_SEARCH_PROVIDER", "").strip().lower()
    endpoint = os.getenv("WEB_SEARCH_ENDPOINT", "").strip() or _DEFAULT_ENDPOINTS.get(provider)
    api_key = os.getenv("WEB_SEARCH_API_KEY", "").strip() or None
    return provider, endpoint, api_key


def _build_provider_request(
    provider: str,
    endpoint: str,
    payload: WebSearchRequest,
    api_key: str | None,
) -> tuple[str, str, dict[str, Any] | None, dict[str, Any] | None, dict[str, str]]:
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
        params: dict[str, Any] = {
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
        source = item.get("source")
        if not source and provider == "brave":
            profile = item.get("profile")
            if isinstance(profile, dict):
                source = profile.get("name")
        results.append(
            WebSearchResult(
                title=str(item.get("title") or item.get("name") or url)[:300],
                url=url,
                snippet=_snippet_for_item(provider, item),
                source=str(source or provider),
                published_at=(
                    str(item.get("published_at") or item.get("date") or item.get("age"))
                    if item.get("published_at") or item.get("date") or item.get("age")
                    else None
                ),
            )
        )
    return results


async def execute_web_search(payload: WebSearchRequest) -> WebSearchResponse:
    provider, endpoint, api_key = _provider_config()
    if not provider:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "web_search_not_configured",
                "message": "WEB_SEARCH_PROVIDER is not configured.",
            },
        )
    if provider not in _SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "web_search_provider_unsupported",
                "message": f"Unsupported WEB_SEARCH_PROVIDER: {provider}",
                "supported": sorted(_SUPPORTED_PROVIDERS),
            },
        )
    if not endpoint:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "web_search_endpoint_missing",
                "message": "WEB_SEARCH_ENDPOINT is required for the configured provider.",
            },
        )

    method, url, params, request_body, headers = _build_provider_request(
        provider,
        endpoint,
        payload,
        api_key,
    )
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if method == "GET":
                response = await client.get(url, params=params, headers=headers)
            else:
                response = await client.post(url, json=request_body, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "web_search_provider_error", "message": str(exc)},
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "error_code": "web_search_provider_rejected",
                "message": response.text[:500],
            },
        )

    data = response.json()
    results = _normalize_results(provider, data, limit=payload.limit)
    return WebSearchResponse(
        query=payload.query,
        provider=provider,
        results=results,
        diagnostics=[] if results else ["provider_returned_no_results"],
    )


@router.post("/query", response_model=WebSearchResponse)
async def query_web(payload: WebSearchRequest) -> WebSearchResponse:
    return await execute_web_search(payload)
