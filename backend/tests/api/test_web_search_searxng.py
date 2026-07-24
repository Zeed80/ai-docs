"""Self-hosted web search: SearXNG normalization, config store, fallback, browse."""

import pytest

from app.api import capability_router, web_search
from app.api.web_search import (
    WebSearchRequest,
    WebSearchResult,
    _build_provider_request,
    _normalize_results,
    execute_web_search,
)


def test_searxng_request_is_keyless_json_get():
    method, url, params, body, headers = _build_provider_request(
        "searxng",
        "http://searxng:8080/search",
        WebSearchRequest(query="сталь 45 прайс", limit=5, recency_days=7),
        api_key=None,
        engines=["google", "bing"],
    )
    assert method == "GET"
    assert body is None
    assert params["q"] == "сталь 45 прайс"
    assert params["format"] == "json"
    assert params["time_range"] == "week"
    assert params["engines"] == "google,bing"
    assert headers == {}  # no API key required


def test_searxng_normalizes_results():
    data = {
        "results": [
            {
                "title": "Прайс на сталь",
                "url": "https://example.ru/steel",
                "content": "Сталь 45 от 100 руб/кг",
                "engine": "google",
                "publishedDate": "2026-07-01",
            },
            {"title": "no url dropped"},  # missing url → skipped
        ]
    }
    results = _normalize_results("searxng", data, limit=5)
    assert len(results) == 1
    assert results[0].url == "https://example.ru/steel"
    assert results[0].snippet == "Сталь 45 от 100 руб/кг"
    assert results[0].source == "google"
    assert results[0].published_at == "2026-07-01"


def test_config_store_masks_secret_and_defaults_searxng(monkeypatch):
    from app.ai import web_search_config as wsc

    # Isolate from any live Redis: force the in-memory/env path.
    store: dict = {}
    monkeypatch.setattr(wsc, "_redis_get", lambda: store or None)
    monkeypatch.setattr(wsc, "_redis_set", lambda d: store.update(d))
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)

    cfg = wsc.get_config()
    assert cfg.provider == "searxng"
    assert cfg.resolved_endpoint() == "http://searxng:8080/search"

    wsc.update_config(
        wsc.WebSearchConfigUpdate(provider="tavily", api_key="sk-secret-123456")
    )
    view = wsc.to_view(wsc.get_config())
    assert view.provider == "tavily"
    assert view.api_key_set is True
    assert "sk-secret-123456" not in view.api_key_mask
    assert view.api_key_mask.endswith("3456")

    # A masked value echoed back must not overwrite the stored secret.
    wsc.update_config(wsc.WebSearchConfigUpdate(api_key=view.api_key_mask))
    assert wsc.get_config().api_key == "sk-secret-123456"


@pytest.mark.asyncio
async def test_execute_falls_back_when_primary_empty(monkeypatch):
    from app.ai import web_search_config as wsc

    cfg = wsc.WebSearchConfig(
        provider="searxng",
        endpoint="http://searxng:8080/search",
        fallback_provider="brave",
        fallback_endpoint="https://api.search.brave.com/res/v1/web/search",
        fallback_api_key="brave-key",
    )
    monkeypatch.setattr(wsc, "get_config", lambda: cfg)
    monkeypatch.setattr(web_search.wsc, "get_config", lambda: cfg)

    calls: list[str] = []

    async def fake_run(provider, endpoint, payload, api_key, engines=None):
        calls.append(provider)
        if provider == "searxng":
            return []  # primary empty → trigger fallback
        return [WebSearchResult(title="hit", url="https://brave.example/x")]

    monkeypatch.setattr(web_search, "_run_provider", fake_run)

    resp = await execute_web_search(WebSearchRequest(query="x"))
    assert calls == ["searxng", "brave"]
    assert resp.provider == "brave"
    assert "used_fallback" in resp.diagnostics
    assert len(resp.results) == 1


def test_browse_action_registered():
    assert capability_router._DISPATCH["search"]["browse"][1] == (
        "/api/web-search/fetch"
    )


def test_research_action_registered():
    assert capability_router._DISPATCH["search"]["research"][1] == (
        "/api/web-search/research"
    )


def test_dedupe_urls_normalizes_and_caps():
    from app.api.web_search import _dedupe_urls

    urls = [
        "https://a.ru/x/",
        "https://a.ru/x",  # dup (trailing slash)
        "https://a.ru/x#frag",  # dup (fragment)
        "https://b.ru/y",
        "https://c.ru/z",
    ]
    assert _dedupe_urls(urls, cap=2) == ["https://a.ru/x/", "https://b.ru/y"]


@pytest.mark.asyncio
async def test_ocr_pdf_pages_joins_vlm_output(monkeypatch):
    import base64

    from app.api import web_search as ws

    class _Resp:
        def __init__(self, text):
            self.text = text

    calls = {"n": 0}

    async def fake_chat(prompt, images, **kw):
        calls["n"] += 1
        return _Resp(f"страница {calls['n']}: Арматура 12 — 71 руб")

    monkeypatch.setattr("app.ai.ollama_client.chat_with_images", fake_chat)
    imgs = [base64.b64encode(b"png1").decode(), base64.b64encode(b"png2").decode()]
    text = await ws._ocr_pdf_pages(imgs, max_chars=10000)
    assert calls["n"] == 2
    assert "страница 1" in text and "страница 2" in text
    assert "Арматура 12" in text


@pytest.mark.asyncio
async def test_research_reads_many_sources_and_publishes(monkeypatch):
    from app.api import web_search as ws

    # Two angles → pooled, de-duplicated result URLs.
    async def fake_search(payload):
        return ws.WebSearchResponse(
            query=payload.query,
            provider="searxng",
            results=[
                ws.WebSearchResult(title="T1", url="https://s1.ru/a", snippet="snip1"),
                ws.WebSearchResult(title="T2", url="https://s2.ru/cat.pdf"),
            ],
        )

    async def fake_fetch(payload):
        if payload.url.endswith(".pdf"):
            return ws.WebFetchResponse(
                url=payload.url, status=200, title="Каталог",
                text="PDF позиции 1..50", diagnostics=["pdf_extracted"],
            )
        return ws.WebFetchResponse(
            url=payload.url, status=200, title="Стр", text="Текст страницы",
        )

    published: dict = {}
    monkeypatch.setattr(ws, "execute_web_search", fake_search)
    monkeypatch.setattr(ws, "fetch_page", fake_fetch)
    monkeypatch.setattr(
        "app.domain.workspace.upsert_workspace_block",
        lambda cid, block: {**block, "id": cid},
    )

    async def fake_publish(msg):
        published.update(msg)

    monkeypatch.setattr("app.core.chat_bus.chat_bus.publish", fake_publish)

    resp = await ws.execute_web_research(
        ws.WebResearchRequest(
            queries=["сталь 45 прайс", "сталь 45 каталог pdf"],
            max_sources=5,
            publish=True,
            canvas_id="web-research-test",
        )
    )
    assert len(resp.sources) == 2
    assert any(s.is_pdf for s in resp.sources)  # PDF source flagged
    assert resp.sources[0].snippet == "snip1"  # snippet carried from search
    assert resp.published_canvas_id == "web-research-test"
    assert published.get("type") == "workspace.updated"
    assert published["block"]["type"] == "markdown"
    assert "Веб-исследование" in published["block"]["content"]
