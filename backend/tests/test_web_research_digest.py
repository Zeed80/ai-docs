"""W0: the model sees every source it read, not the first one.

``research`` answered with full page texts under ``sources``; neither the vault
nor the history trimmer knew that key, so five pages were cut by characters
and the model read about one. The answer now carries passages and a
``source_ref`` per source, and the trimmer samples lists inside ToolResult v1.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.ai.agent_loop import _MAX_TOOL_RESULT_CHARS, _trim_tool_result
from app.api import web_search as ws


def _page(text: str, *, status: int = 200, diagnostics: list[str] | None = None):
    return {
        "final_url": None,
        "status": status,
        "title": "Насос",
        "text": text,
        "diagnostics": diagnostics or [],
    }


def test_passages_lead_with_the_query_terms():
    filler = "Доставка по всей России, звоните. " * 60
    text = filler + "Насос КМ 80-50-200: подача 50 м3/ч, напор 50 м. " + filler
    passages = ws.extract_passages(text, "подача насоса КМ 80-50-200", 900)
    assert passages
    assert any("подача 50 м3/ч" in p.text for p in passages)
    assert sum(len(p.text) for p in passages) <= 900


def test_without_matches_the_page_opening_is_the_passage():
    passages = ws.extract_passages("Первая строка страницы. " * 50, "титан", 600)
    assert len(passages) == 1 and passages[0].offset == 0


@pytest.mark.parametrize(
    ("status", "text", "diagnostics", "expected"),
    [
        (200, "текст", [], "ok"),
        (200, "Just a moment...", ["bot_challenge_detected"], "blocked"),
        (403, "Forbidden", [], "error"),
        (200, "   ", [], "empty"),
    ],
)
def test_read_status(status, text, diagnostics, expected):
    assert ws.classify_read(status, text, diagnostics) == expected


@pytest.mark.asyncio
async def test_research_answer_fits_the_history_with_every_source(client: AsyncClient, monkeypatch):
    from app.ai import web_search_config as wsc

    monkeypatch.setattr(
        wsc, "get_config", lambda: wsc.WebSearchConfig(provider="searxng", per_host_interval_s=0)
    )

    def long_text(i: int) -> str:
        topic = ["завода", "склада", "дилера", "сервиса"][i]
        return (f"Общие сведения о продукции {topic} номер {i}. " * 400) + "Масса насоса 120 кг."

    pages = {f"https://s{i}.example/p": _page(long_text(i)) for i in range(4)} | {
        "https://wall.example/p": _page("Just a moment", diagnostics=["bot_challenge_detected"]),
        # A mirror of the first page: read, but not counted twice.
        "https://mirror.example/p": _page(long_text(0)),
    }

    class _Resp:
        def __init__(self, data):
            self.status_code = 200
            self._data = data
            self.text = ""

        def json(self):
            return self._data

    real_post = AsyncClient.post

    async def fake_post(self, url, json=None, **kwargs):  # noqa: A002
        if not str(url).endswith("/fetch"):  # the test client itself
            return await real_post(self, url, json=json, **kwargs)
        return _Resp(pages[json["url"]])

    search = ws.WebSearchResponse(
        query="масса насоса",
        provider="searxng",
        results=[ws.WebSearchResult(title=u, url=u) for u in pages],
    )
    with (
        patch.object(ws, "execute_web_search", AsyncMock(return_value=search)),
        patch("httpx.AsyncClient.post", fake_post),
    ):
        resp = await client.post(
            "/api/web-search/research",
            json={"query": "масса насоса", "max_sources": 6, "fresh": True},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert [s["n"] for s in body["sources"]] == [1, 2, 3, 4, 5, 6]
    statuses = {s["url"]: s["read_status"] for s in body["sources"]}
    assert statuses["https://wall.example/p"] == "blocked"
    mirror = next(s for s in body["sources"] if s["url"] == "https://mirror.example/p")
    assert mirror["duplicate_of"] == 1 and mirror["passages"] == []
    readable = [
        s for s in body["sources"] if s["read_status"] == "ok" and s["duplicate_of"] is None
    ]
    assert len(readable) == 4
    assert all(s["text"] == "" and s["source_ref"] for s in readable)
    assert all(any("120 кг" in p["text"] for p in s["passages"]) for s in readable)

    # What the model gets: the ToolResult envelope through the history trimmer.
    envelope = json.dumps({"version": 1, "status": "ok", "data": body}, ensure_ascii=False)
    shown = json.loads(_trim_tool_result(envelope))
    assert len(json.dumps(shown, ensure_ascii=False)) <= _MAX_TOOL_RESULT_CHARS
    assert len(shown["data"]["sources"]) == 6

    # The rest of a page is read on without fetching it again.
    ref = readable[0]["source_ref"]
    more = await client.post(
        "/api/web-search/source", json={"source_ref": ref, "offset": 0, "max_chars": 500}
    )
    assert more.status_code == 200
    assert more.json()["next_offset"] == 500
    assert more.json()["text_chars"] == len(long_text(0))


@pytest.mark.asyncio
async def test_read_source_is_owner_scoped(client: AsyncClient):
    ref = await ws.store_source("https://a.example", "A", "секретный текст")
    with patch("app.domain.workspace._owner", return_value="someone-else"):
        resp = await client.post("/api/web-search/source", json={"source_ref": ref})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "web_source_expired"


def test_trimmer_samples_lists_inside_a_tool_result_envelope():
    items = [{"id": i, "name": "x" * 400} for i in range(40)]
    envelope = json.dumps({"version": 1, "status": "ok", "data": {"items": items, "total": 40}})
    shown = json.loads(_trim_tool_result(envelope))
    assert shown["version"] == 1 and shown["status"] == "ok"
    assert 3 <= len(shown["data"]["items"]) < 40
    assert "total=40" in shown["data"]["_note"]
