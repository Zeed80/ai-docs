"""Web search provider adapters.

Every engine the agent can search through is one adapter here: it builds the
request from a provider entry of the web-search config and turns the answer
into :class:`WebSearchResult` rows. A new engine is a new adapter plus an entry
in the GUI — callers (:mod:`app.api.web_search`) never branch on the kind.

HTTP adapters: searxng (self-hosted, keyless), tavily, serper, brave, exa,
jina, yandex (Yandex Cloud Search API v2) and ``custom`` (our own JSON shape).
The ``mcp`` adapter calls a search tool of a configured MCP server, so any
MCP search server (Tavily, Brave, Kagi, …) plugs in without code.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from xml.etree import ElementTree

import httpx
from pydantic import BaseModel


class WebSearchResult(BaseModel):
    title: str
    url: str
    snippet: str | None = None
    source: str | None = None
    published_at: str | None = None
    # Provider ids that returned this page (more than one after fusion).
    found_by: list[str] = []


class SearchQuery(Protocol):
    query: str
    limit: int
    recency_days: int | None
    domains: list[str] | None
    intent: str | None
    language: str | None


class ProviderRejected(Exception):
    """The provider answered, but with an error (HTTP 4xx/5xx, RPC error)."""


KINDS = ("searxng", "tavily", "serper", "brave", "exa", "jina", "yandex", "custom", "mcp")
# Kinds that need an API key to answer at all.
KEYED_KINDS = frozenset({"tavily", "serper", "brave", "exa", "yandex", "custom"})

DEFAULT_ENDPOINTS = {
    "searxng": "http://searxng:8080/search",
    "tavily": "https://api.tavily.com/search",
    "serper": "https://google.serper.dev/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "exa": "https://api.exa.ai/search",
    "jina": "https://s.jina.ai/",
    "yandex": "https://searchapi.api.cloud.yandex.net/v2/web/search",
}


class ProviderSpec(Protocol):
    kind: str
    api_key: str
    engines: list[str]
    region: str | None
    folder_id: str | None
    mcp_tool: str | None

    def resolved_endpoint(self) -> str | None: ...


# ── request helpers ──────────────────────────────────────────────────────────


def _recency_bucket(days: int | None, buckets: tuple[tuple[int, str], ...]) -> str | None:
    if not days:
        return None
    for limit, name in buckets:
        if days <= limit:
            return name
    return None


_DAY_WEEK_MONTH_YEAR = ((1, "day"), (7, "week"), (31, "month"), (366, "year"))


def query_with_domains(query: str, domains: list[str] | None) -> str:
    clean = [d.strip() for d in domains or [] if d and d.strip()]
    if not clean:
        return query
    return f"{query} " + " OR ".join(f"site:{domain}" for domain in clean)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


Request = tuple[str, str, dict[str, Any] | None, dict[str, Any] | None, dict[str, str]]


def build_request(spec: ProviderSpec, q: SearchQuery) -> Request:
    """(method, url, params, json body, headers) for an HTTP provider."""
    kind, key, endpoint = spec.kind, spec.api_key, spec.resolved_endpoint() or ""
    if kind == "searxng":
        params: dict[str, Any] = {
            "q": query_with_domains(q.query, q.domains),
            "format": "json",
            "safesearch": 0,
        }
        if q.language:
            params["language"] = q.language
        time_range = _recency_bucket(q.recency_days, _DAY_WEEK_MONTH_YEAR) or (
            "year" if q.recency_days else None
        )
        if time_range:
            params["time_range"] = time_range
        if spec.engines:
            params["engines"] = ",".join(spec.engines)
        return "GET", endpoint, params, None, _bearer(key)

    if kind == "tavily":
        body: dict[str, Any] = {
            "query": q.query,
            "max_results": q.limit,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        time_range = _recency_bucket(q.recency_days, _DAY_WEEK_MONTH_YEAR)
        if time_range:
            body["time_range"] = time_range
        if q.domains:
            body["include_domains"] = q.domains
        return "POST", endpoint, None, body, _bearer(key)

    if kind == "serper":
        body = {"q": query_with_domains(q.query, q.domains), "num": q.limit}
        if q.recency_days:
            body["tbs"] = f"qdr:d{q.recency_days}"
        if q.language:
            body["hl"] = q.language
        if spec.region:
            body["gl"] = spec.region
        return "POST", endpoint, None, body, {"X-API-KEY": key} if key else {}

    if kind == "brave":
        params = {
            "q": query_with_domains(q.query, q.domains),
            "count": q.limit,
            "extra_snippets": "true",
        }
        freshness = _recency_bucket(q.recency_days, ((1, "pd"), (7, "pw"), (31, "pm"), (366, "py")))
        if freshness:
            params["freshness"] = freshness
        if q.language:
            params["search_lang"] = q.language
        if spec.region:
            params["country"] = spec.region
        return "GET", endpoint, params, None, {"X-Subscription-Token": key} if key else {}

    if kind == "exa":
        body = {
            "query": q.query,
            "numResults": q.limit,
            "contents": {"highlights": True},
        }
        if q.domains:
            body["includeDomains"] = q.domains
        if q.recency_days:
            since = datetime.now(UTC) - timedelta(days=q.recency_days)
            body["startPublishedDate"] = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if spec.region:
            body["userLocation"] = spec.region
        return "POST", endpoint, None, body, {"x-api-key": key} if key else {}

    if kind == "jina":
        headers = {"Accept": "application/json", "X-Respond-With": "no-content", **_bearer(key)}
        if q.domains:
            headers["X-Site"] = q.domains[0]
        params = {"q": q.query}
        if q.language:
            params["hl"] = q.language
        if spec.region:
            params["gl"] = spec.region
        return "GET", endpoint, params, None, headers

    if kind == "yandex":
        body = {
            "query": {
                "searchType": f"SEARCH_TYPE_{(spec.region or 'ru').upper()}",
                "queryText": query_with_domains(q.query, q.domains)[:400],
                "familyMode": "FAMILY_MODE_NONE",
            },
            "groupSpec": {
                "groupMode": "GROUP_MODE_FLAT",
                "groupsOnPage": str(q.limit),
                "docsInGroup": "1",
            },
            "maxPassages": "3",
            "responseFormat": "FORMAT_XML",
        }
        if spec.folder_id:
            body["folderId"] = spec.folder_id
        if q.language:
            body["l10n"] = f"LOCALIZATION_{q.language.upper()}"
        period = _recency_bucket(
            q.recency_days, ((1, "PERIOD_DAY"), (14, "PERIOD_2_WEEKS"), (31, "PERIOD_MONTH"))
        )
        if period:
            body["period"] = period
        return "POST", endpoint, None, body, {"Authorization": f"Api-Key {key}"} if key else {}

    body = {
        "query": q.query,
        "limit": q.limit,
        "recency_days": q.recency_days,
        "domains": q.domains,
        "intent": q.intent,
        "language": q.language,
    }
    return "POST", endpoint, None, body, _bearer(key)


# ── response parsing ─────────────────────────────────────────────────────────


def _yandex_items(data: dict) -> list[dict]:
    raw = data.get("rawData")
    if not isinstance(raw, str) or not raw:
        return []
    try:
        root = ElementTree.fromstring(base64.b64decode(raw))
    except (ValueError, ElementTree.ParseError):
        return []

    def _text(node) -> str:
        return " ".join("".join(node.itertext()).split()) if node is not None else ""

    items = []
    for doc in root.iter("doc"):
        passages = [_text(p) for p in doc.iter("passage")]
        headline = _text(doc.find("headline"))
        items.append(
            {
                "url": _text(doc.find("url")),
                "title": _text(doc.find("title")),
                "snippet": " … ".join(p for p in passages if p) or headline,
                "source": _text(doc.find("domain")) or "yandex",
                "published_at": _text(doc.find("modtime")) or None,
            }
        )
    return items


def raw_items(kind: str, data: Any) -> list[Any]:
    if not isinstance(data, dict):
        return data if isinstance(data, list) else []
    if kind == "serper":
        return list(data.get("organic") or data.get("results") or [])
    if kind == "brave":
        web = data.get("web") if isinstance(data.get("web"), dict) else {}
        return list(web.get("results") or [])
    if kind == "jina":
        return list(data.get("data") or [])
    if kind == "yandex":
        return _yandex_items(data)
    return list(data.get("results") or data.get("items") or [])


def _snippet(kind: str, item: dict[str, Any]) -> str | None:
    snippet = item.get("snippet") or item.get("content") or item.get("description")
    if kind == "brave":
        extra = item.get("extra_snippets")
        if isinstance(extra, list) and extra:
            pieces = [str(snippet)] if snippet else []
            pieces.extend(str(part) for part in extra[:2] if part)
            snippet = " ".join(pieces)
    if kind == "exa" and not snippet:
        highlights = item.get("highlights")
        if isinstance(highlights, list):
            snippet = " … ".join(str(h) for h in highlights[:2] if h)
    return str(snippet)[:1000] if snippet else None


_YANDEX_MODTIME = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})$")


def parse_published(value: Any) -> str | None:
    """ISO date when the provider gave one; a relative age ("2 days ago") stays as is."""
    if not value:
        return None
    text = str(value).strip()
    match = _YANDEX_MODTIME.match(text)
    if match:
        y, mo, d, h, mi, s = match.groups()
        return f"{y}-{mo}-{d}T{h}:{mi}:{s}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return text[:40]


def normalize(kind: str, data: Any, *, limit: int) -> list[WebSearchResult]:
    results: list[WebSearchResult] = []
    for item in raw_items(kind, data):
        if len(results) >= limit:
            break
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or "")
        if not url:
            continue
        source = item.get("source") or item.get("engine")
        if not source and kind == "brave":
            profile = item.get("profile")
            if isinstance(profile, dict):
                source = profile.get("name")
        published = (
            item.get("published_at")
            or item.get("publishedDate")
            or item.get("date")
            or item.get("age")
        )
        results.append(
            WebSearchResult(
                title=str(item.get("title") or item.get("name") or url)[:300],
                url=url,
                snippet=_snippet(kind, item),
                source=str(source or kind),
                published_at=parse_published(published),
            )
        )
    return results


# ── MCP search tools ─────────────────────────────────────────────────────────

_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def parse_mcp_text(text: str, *, limit: int) -> list[WebSearchResult]:
    """Results from an MCP search tool: JSON when it returns JSON, else URLs in text.

    MCP servers answer with text. Most search servers put JSON there; others
    print "Title: … / URL: … / Content: …" blocks — then the line before the
    URL is the title and the lines after it are the snippet.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None
    if data is not None:
        return normalize("mcp", data, limit=limit)
    results: list[WebSearchResult] = []
    lines = [line.strip() for line in (text or "").splitlines()]
    for i, line in enumerate(lines):
        match = _URL.search(line)
        if not match or any(r.url == match.group(0) for r in results):
            continue
        title = re.sub(r"^(title|заголовок)\s*:\s*", "", lines[i - 1], flags=re.I) if i else ""
        following = []
        for nxt in lines[i + 1 : i + 4]:
            if _URL.search(nxt) or not nxt:
                break
            following.append(re.sub(r"^(content|snippet|описание)\s*:\s*", "", nxt, flags=re.I))
        results.append(
            WebSearchResult(
                title=(title or match.group(0))[:300],
                url=match.group(0),
                snippet=" ".join(following)[:1000] or None,
                source="mcp",
            )
        )
        if len(results) >= limit:
            break
    return results


async def _run_mcp(spec: ProviderSpec, q: SearchQuery) -> list[WebSearchResult]:
    from app.ai.mcp_capability import get_mcp_tool_handler

    handler = await get_mcp_tool_handler(spec.mcp_tool or "")
    if handler is None:
        raise ProviderRejected(f"mcp tool not found: {spec.mcp_tool}")
    call = handler.get("_handler") if isinstance(handler, dict) else handler
    args: dict[str, Any] = {"query": query_with_domains(q.query, q.domains)}
    answer = await call(args)
    if isinstance(answer, dict) and answer.get("error"):
        raise ProviderRejected(str(answer["error"])[:200])
    text = answer.get("result") if isinstance(answer, dict) else answer
    if not isinstance(text, str):
        return normalize("mcp", answer, limit=q.limit)
    return parse_mcp_text(text, limit=q.limit)


async def run(spec: ProviderSpec, q: SearchQuery) -> list[WebSearchResult]:
    """Query one provider. Raises ProviderRejected or httpx.HTTPError on failure."""
    if spec.kind == "mcp":
        return await _run_mcp(spec, q)
    method, url, params, body, headers = build_request(spec, q)
    if not url:
        raise ProviderRejected("endpoint not configured")
    async with httpx.AsyncClient(timeout=20.0) as client:
        if method == "GET":
            response = await client.get(url, params=params, headers=headers)
        else:
            response = await client.post(url, json=body, headers=headers)
    if response.status_code >= 400:
        raise ProviderRejected(f"HTTP {response.status_code}: {response.text[:200]}")
    return normalize(spec.kind, response.json(), limit=q.limit)


# ── fusion ───────────────────────────────────────────────────────────────────

RRF_K = 60


def fuse(
    ranked: list[tuple[str, float, list[WebSearchResult]]], limit: int
) -> list[WebSearchResult]:
    """Reciprocal Rank Fusion of several providers' lists.

    ``ranked`` is (provider id, weight, results). A page found by two engines
    rises above one only one engine liked; duplicates are folded by
    :func:`app.ai.web_urls.url_key`, the earliest snippet and date kept.
    """
    from app.ai.web_urls import url_key

    scores: dict[str, float] = {}
    best: dict[str, WebSearchResult] = {}
    found_by: dict[str, list[str]] = {}
    for provider_id, weight, results in ranked:
        for rank, result in enumerate(results):
            key = url_key(result.url)
            scores[key] = scores.get(key, 0.0) + weight / (RRF_K + rank + 1)
            found_by.setdefault(key, [])
            if provider_id not in found_by[key]:
                found_by[key].append(provider_id)
            current = best.get(key)
            if current is None:
                best[key] = result.model_copy()
            else:
                if not current.snippet and result.snippet:
                    current.snippet = result.snippet
                if not current.published_at and result.published_at:
                    current.published_at = result.published_at
    order = sorted(scores, key=lambda key: -scores[key])[:limit]
    fused = []
    for key in order:
        best[key].found_by = found_by[key]
        fused.append(best[key])
    return fused
