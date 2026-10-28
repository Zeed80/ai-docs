"""W1: any search engine, fused results, cache, quotas and a polite pace."""

from __future__ import annotations

import base64
import time

import pytest

from app.ai import web_providers as wp
from app.ai import web_search_config as wsc
from app.ai.web_urls import dedupe, near_duplicate, simhash, url_key
from app.api import web_search as ws


def _hit(url: str, **kw) -> wp.WebSearchResult:
    return wp.WebSearchResult(title=url, url=url, **kw)


# ── URL identity ─────────────────────────────────────────────────────────────


def test_url_key_folds_tracking_scheme_and_mirrors():
    base = url_key("https://pumps.example/km/80-50?model=1")
    assert url_key("http://www.pumps.example/km/80-50/?model=1&utm_source=ya&yclid=7") == base
    assert url_key("https://m.pumps.example/km/80-50?model=1#specs") == base
    assert url_key("https://pumps.example/km/80-50/amp?model=1") == base
    # A content parameter is part of the page.
    assert url_key("https://pumps.example/km/80-50?model=2") != base


def test_dedupe_keeps_first_and_caps():
    urls = [
        "https://a.ru/x?utm_medium=cpc",
        "https://www.a.ru/x",
        "https://b.ru/y",
        "https://c.ru/z",
    ]
    assert dedupe(urls, cap=2) == ["https://a.ru/x?utm_medium=cpc", "https://b.ru/y"]


def test_simhash_marks_mirrors_not_neighbours():
    text = "Насос КМ 80-50-200 подача 50 м3/ч напор 50 м мощность 15 кВт масса 120 кг " * 20
    assert near_duplicate(simhash(text), simhash(text + " Распечатать"))
    other = "Насос КМ 100-80-160 подача 100 м3/ч напор 32 м мощность 15 кВт масса 150 кг " * 20
    assert not near_duplicate(simhash(text), simhash(other))


# ── fusion ───────────────────────────────────────────────────────────────────


def test_rrf_raises_a_page_two_engines_found():
    fused = wp.fuse(
        [
            ("searxng", 1.0, [_hit("https://a.example/1"), _hit("https://b.example/2")]),
            ("yandex", 1.0, [_hit("https://www.b.example/2/", snippet="из Яндекса")]),
        ],
        limit=5,
    )
    assert fused[0].url == "https://b.example/2"
    assert fused[0].found_by == ["searxng", "yandex"]
    assert fused[0].snippet == "из Яндекса"
    assert [r.url for r in fused] == ["https://b.example/2", "https://a.example/1"]


# ── adapters ─────────────────────────────────────────────────────────────────


class _Q:
    def __init__(self, **kw):
        self.query = kw.get("query", "насос КМ")
        self.limit = kw.get("limit", 5)
        self.recency_days = kw.get("recency_days")
        self.domains = kw.get("domains")
        self.intent = None
        self.language = kw.get("language", "ru")


def test_yandex_request_and_xml_answer():
    spec = wsc.SearchProvider(id="ya", kind="yandex", api_key="AQV", folder_id="b1g", region="ru")
    method, url, _params, body, headers = wp.build_request(spec, _Q(recency_days=7))
    assert method == "POST" and url.endswith("/v2/web/search")
    assert headers == {"Authorization": "Api-Key AQV"}
    assert body["folderId"] == "b1g"
    assert body["query"]["searchType"] == "SEARCH_TYPE_RU"
    assert body["period"] == "PERIOD_2_WEEKS"
    assert body["l10n"] == "LOCALIZATION_RU"

    xml = (
        "<yandexsearch><response><results><grouping><group><doc>"
        "<url>https://pumps.example/km</url><domain>pumps.example</domain>"
        "<title>Насос <hlword>КМ</hlword> 80-50-200</title><modtime>20260115T101500</modtime>"
        "<passages><passage>Подача 50 м3/ч</passage><passage>Напор 50 м</passage></passages>"
        "</doc></group></grouping></results></response></yandexsearch>"
    )
    rows = wp.normalize("yandex", {"rawData": base64.b64encode(xml.encode()).decode()}, limit=5)
    assert rows[0].title == "Насос КМ 80-50-200"
    assert rows[0].snippet == "Подача 50 м3/ч … Напор 50 м"
    assert rows[0].source == "pumps.example"
    assert rows[0].published_at == "2026-01-15T10:15:00"


def test_exa_and_jina_requests():
    exa = wsc.SearchProvider(id="exa", kind="exa", api_key="k")
    _m, url, _p, body, headers = wp.build_request(
        exa, _Q(domains=["pumps.example"], recency_days=30)
    )
    assert url == "https://api.exa.ai/search" and headers == {"x-api-key": "k"}
    assert body["includeDomains"] == ["pumps.example"] and "startPublishedDate" in body
    rows = wp.normalize(
        "exa",
        {"results": [{"url": "https://e.x", "title": "E", "highlights": ["h1", "h2"]}]},
        limit=3,
    )
    assert rows[0].snippet == "h1 … h2"

    jina = wsc.SearchProvider(id="jina", kind="jina", api_key="j")
    method, _url, params, _b, headers = wp.build_request(jina, _Q())
    assert method == "GET" and params["q"] == "насос КМ"
    assert headers["Accept"] == "application/json" and headers["Authorization"] == "Bearer j"
    rows = wp.normalize(
        "jina", {"data": [{"url": "https://j.x", "title": "J", "description": "d"}]}, limit=3
    )
    assert rows[0].snippet == "d"


def test_mcp_text_answers_are_parsed():
    as_json = '{"results": [{"url": "https://m.x/1", "title": "One", "content": "c"}]}'
    assert wp.parse_mcp_text(as_json, limit=5)[0].url == "https://m.x/1"
    printed = (
        "Title: Насос КМ\nURL: https://pumps.example/km\nContent: подача 50 м3/ч\n\n"
        "Title: Другой\nURL: https://other.example/x\nContent: что-то"
    )
    rows = wp.parse_mcp_text(printed, limit=5)
    assert [(r.title, r.url, r.snippet) for r in rows] == [
        ("Насос КМ", "https://pumps.example/km", "подача 50 м3/ч"),
        ("Другой", "https://other.example/x", "что-то"),
    ]


# ── execute_web_search ───────────────────────────────────────────────────────


def _cfg(*providers: wsc.SearchProvider) -> wsc.WebSearchConfig:
    return wsc.WebSearchConfig(providers=list(providers), per_host_interval_s=0)


@pytest.mark.asyncio
async def test_primaries_are_fused_and_a_failure_is_a_diagnostic(monkeypatch):
    cfg = _cfg(
        wsc.SearchProvider(id="sx", kind="searxng"),
        wsc.SearchProvider(id="br", kind="brave", api_key="k"),
        wsc.SearchProvider(id="down", kind="tavily", api_key="k"),
    )
    monkeypatch.setattr(ws.wsc, "get_config", lambda: cfg)

    async def fake(spec, q):
        if spec.id == "down":
            raise wp.ProviderRejected("HTTP 500")
        return [_hit(f"https://{spec.id}.example/a"), _hit("https://both.example/p")]

    monkeypatch.setattr(ws, "_query_provider", fake)
    resp = await ws.execute_web_search(ws.WebSearchRequest(query="насос", fresh=True))
    assert resp.results[0].url == "https://both.example/p"
    assert resp.providers_used == ["sx", "br"]
    assert any(d.startswith("down_rejected") for d in resp.diagnostics)


@pytest.mark.asyncio
async def test_unknown_provider_is_refused_not_swapped(monkeypatch):
    monkeypatch.setattr(
        ws.wsc, "get_config", lambda: _cfg(wsc.SearchProvider(id="sx", kind="searxng"))
    )
    with pytest.raises(ws.HTTPException) as err:
        await ws.execute_web_search(ws.WebSearchRequest(query="x", providers=["exa"]))
    assert err.value.detail["error_code"] == "web_search_provider_unknown"


@pytest.mark.asyncio
async def test_daily_quota_is_counted_before_the_call(monkeypatch):
    cfg = _cfg(wsc.SearchProvider(id="paid", kind="serper", api_key="k", daily_quota=1))
    monkeypatch.setattr(ws.wsc, "get_config", lambda: cfg)
    calls = []

    async def fake(spec, q):
        calls.append(spec.id)
        return []

    monkeypatch.setattr(ws, "_query_provider", fake)
    await ws.execute_web_search(ws.WebSearchRequest(query="a", fresh=True))
    second = await ws.execute_web_search(ws.WebSearchRequest(query="b", fresh=True))
    assert calls == ["paid"]
    assert "quota_exhausted:paid" in second.diagnostics


@pytest.mark.asyncio
async def test_results_are_cached_until_fresh(monkeypatch):
    cfg = _cfg(wsc.SearchProvider(id="sx", kind="searxng"))
    monkeypatch.setattr(ws.wsc, "get_config", lambda: cfg)
    calls = []

    async def fake(spec, q):
        calls.append(q.query)
        return [_hit("https://a.example/1")]

    monkeypatch.setattr(ws, "_query_provider", fake)
    query = f"кэш {time.time()}"
    await ws.execute_web_search(ws.WebSearchRequest(query=query))
    again = await ws.execute_web_search(ws.WebSearchRequest(query=query))
    assert calls == [query] and "cached" in again.diagnostics and again.cached_at
    await ws.execute_web_search(ws.WebSearchRequest(query=query, fresh=True))
    assert calls == [query, query]


@pytest.mark.asyncio
async def test_one_host_is_paced(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ws.asyncio, "sleep", fake_sleep)
    url = f"https://paced-{time.time()}.example/page"
    assert await ws.wait_for_host_slot(url, 2.0) == 0.0
    second = await ws.wait_for_host_slot(url.replace("/page", "/other"), 2.0)
    assert 1.9 <= second <= 3.3
    assert await ws.wait_for_host_slot("https://another-host.example/", 2.0) == 0.0


# ── settings ─────────────────────────────────────────────────────────────────


def test_provider_list_keeps_a_key_not_re_entered():
    wsc.update_config(
        wsc.WebSearchConfigUpdate(
            providers=[
                wsc.SearchProviderUpdate(id="sx", kind="searxng"),
                wsc.SearchProviderUpdate(
                    id="ya", kind="yandex", api_key="AQV-secret", folder_id="b1g"
                ),
            ]
        )
    )
    view = wsc.to_view(wsc.get_config())
    ya = next(p for p in view.providers if p.id == "ya")
    assert view.providers_saved and ya.api_key_set and "secret" not in ya.api_key_mask

    wsc.update_config(
        wsc.WebSearchConfigUpdate(
            providers=[
                wsc.SearchProviderUpdate(
                    id="ya", kind="yandex", api_key=ya.api_key_mask, weight=2.0
                )
            ]
        )
    )
    cfg = wsc.get_config()
    assert [p.id for p in cfg.effective_providers()] == ["ya"]
    assert cfg.providers[0].api_key == "AQV-secret" and cfg.providers[0].weight == 2.0


def test_provider_update_validates_kind_and_id():
    with pytest.raises(ValueError):
        wsc.SearchProviderUpdate(id="x", kind="altavista")
    with pytest.raises(ValueError):
        wsc.SearchProviderUpdate(id="Пробел здесь", kind="searxng")
