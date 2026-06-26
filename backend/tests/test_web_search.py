from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.api.web_search import (
    WebSearchRequest,
    _build_provider_request,
    _normalize_results,
)


@pytest.mark.asyncio
async def test_web_search_requires_configured_provider(client: AsyncClient, monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)

    resp = await client.post("/api/web-search/query", json={"query": "каталог поставщика"})

    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "web_search_not_configured"


def test_tavily_request_uses_native_fields():
    method, url, params, body, headers = _build_provider_request(
        "tavily",
        "https://api.tavily.com/search",
        WebSearchRequest(
            query="каталог АКМЕ",
            limit=7,
            recency_days=14,
            domains=["acme.example"],
        ),
        "tvly-key",
    )

    assert method == "POST"
    assert url == "https://api.tavily.com/search"
    assert params is None
    assert body["query"] == "каталог АКМЕ"
    assert body["max_results"] == 7
    assert body["time_range"] == "month"
    assert body["include_domains"] == ["acme.example"]
    assert headers["Authorization"] == "Bearer tvly-key"


def test_serper_request_uses_query_operators_and_api_key_header():
    method, _url, params, body, headers = _build_provider_request(
        "serper",
        "https://google.serper.dev/search",
        WebSearchRequest(
            query="прайс лист",
            limit=3,
            recency_days=5,
            domains=["supplier.example", "catalog.example"],
        ),
        "serper-key",
    )

    assert method == "POST"
    assert params is None
    assert body["q"] == "прайс лист site:supplier.example OR site:catalog.example"
    assert body["num"] == 3
    assert body["tbs"] == "qdr:d5"
    assert headers["X-API-KEY"] == "serper-key"


def test_brave_request_uses_get_params_and_subscription_header():
    method, _url, params, body, headers = _build_provider_request(
        "brave",
        "https://api.search.brave.com/res/v1/web/search",
        WebSearchRequest(
            query="manufacturer catalog",
            limit=4,
            recency_days=7,
            domains=["maker.example"],
        ),
        "brave-key",
    )

    assert method == "GET"
    assert body is None
    assert params["q"] == "manufacturer catalog site:maker.example"
    assert params["count"] == 4
    assert params["freshness"] == "pw"
    assert headers["X-Subscription-Token"] == "brave-key"


def test_normalizes_provider_specific_results():
    tavily = _normalize_results(
        "tavily",
        {"results": [{"title": "T", "url": "https://t.example", "content": "body"}]},
        limit=5,
    )
    serper = _normalize_results(
        "serper",
        {"organic": [{"title": "S", "link": "https://s.example", "snippet": "snip"}]},
        limit=5,
    )
    brave = _normalize_results(
        "brave",
        {
            "web": {
                "results": [
                    {
                        "title": "B",
                        "url": "https://b.example",
                        "description": "desc",
                        "extra_snippets": ["extra"],
                        "profile": {"name": "Brave Source"},
                    }
                ]
            }
        },
        limit=5,
    )

    assert tavily[0].snippet == "body"
    assert serper[0].url == "https://s.example"
    assert brave[0].source == "Brave Source"
    assert brave[0].snippet == "desc extra"
