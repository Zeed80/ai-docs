"""W4: the brief, rounds until it is covered, facts that stand on the page."""

from __future__ import annotations

import pytest

from app.ai import web_research as wr
from app.api import web_search as ws

PUMP = "Насос КМ 80-50-200"


def _brief(**kw) -> wr.ResearchBrief:
    data = {
        "goal": "Паспортные характеристики насоса КМ 80-50-200",
        "entities": [PUMP],
        "facts_needed": [
            {"name": "подача", "unit": "м3/ч", "why": None},
            {"name": "напор", "unit": "м", "why": None},
            {"name": "масса", "unit": "кг", "why": None},
        ],
        "source_preference": ["manufacturer", "distributor", "marketplace"],
        "queries": [{"text": "КМ 80-50-200 характеристики", "language": "ru", "site": None}],
        "stop_when": "все три значения",
        "ambiguities": [],
        "must_clarify": False,
    }
    data.update(kw)
    return wr.ResearchBrief.model_validate(data)


# ── facts must stand on the page ─────────────────────────────────────────────


PAGE = (
    "Насос КМ 80-50-200. Подача (номин.), м³/ч: 50. Напор (номин.), м: 50. Масса агрегата 120 кг."
)


def _fact(value, quote, name="масса"):
    return wr.PageFact(name=name, value=value, unit="кг", quote=quote)


def test_a_fact_with_its_quote_on_the_page_stands():
    assert wr.verify_fact(_fact("120", "Масса агрегата 120 кг"), PAGE) is None
    # Edge punctuation and case do not matter.
    assert wr.verify_fact(_fact("120", "«масса агрегата 120 кг.»"), PAGE) is None


def test_an_invented_quote_or_value_is_dropped():
    assert wr.verify_fact(_fact("95", "Масса агрегата 95 кг"), PAGE) == "quote_not_on_page"
    assert wr.verify_fact(_fact("95", "Масса агрегата 120 кг"), PAGE) == "value_not_in_quote"
    assert wr.verify_fact(_fact("120", "120"), PAGE) == "quote_too_short"


# ── queries do not carry the company's data ──────────────────────────────────


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("насос для ООО Ромашка ИНН 7701234567", "inn"),
        ("поставщик ivanov@factory.ru", "email"),
        ("насос КМ цена 125 000 руб", "amount"),
        ("звонить +7 495 123-45-67", "phone"),
    ],
)
def test_leaks_are_detected(text, leak):
    assert leak in wr.query_leaks(text)


def test_a_model_number_is_not_a_leak():
    assert wr.query_leaks("КМ 80-50-200 паспорт pdf") == []


def test_the_brief_drops_leaking_queries():
    brief = _brief(
        queries=[
            {"text": "КМ 80-50-200 паспорт", "language": "ru", "site": "WWW.hms.ru/catalog"},
            {"text": "КМ 80-50-200 для счёта 4470 на 125 000 руб", "language": "ru", "site": None},
        ],
        source_preference=["manufacturer", "nonsense"],
    )
    bounded, diagnostics = wr._bound_brief(brief)
    assert [q.text for q in bounded.queries] == ["КМ 80-50-200 паспорт"]
    assert bounded.queries[0].site == "hms.ru"
    assert bounded.source_preference == ["manufacturer"]
    assert diagnostics == ["query_leak_dropped:amount"]


# ── agreement ────────────────────────────────────────────────────────────────


def test_agreement_between_sites():
    found = [
        {
            "name": "подача",
            "value": "50",
            "unit": "м3/ч",
            "quote": "q1",
            "n": 1,
            "domain": "hms.ru",
        },
        {
            "name": "подача",
            "value": "50,0",
            "unit": "м3/ч",
            "quote": "q2",
            "n": 2,
            "domain": "pump.ru",
        },
        {"name": "напор", "value": "50", "unit": "м", "quote": "q3", "n": 1, "domain": "hms.ru"},
        {"name": "масса", "value": "120", "unit": "кг", "quote": "q4", "n": 1, "domain": "hms.ru"},
        {"name": "масса", "value": "135", "unit": "кг", "quote": "q5", "n": 3, "domain": "shop.ru"},
    ]
    rows = {r["name"]: r for r in wr.aggregate_facts(_brief(), found)}
    assert rows["подача"]["status"] == "confirmed" and rows["подача"]["values"][0]["sources"] == [
        1,
        2,
    ]
    assert rows["напор"]["status"] == "single"
    assert rows["масса"]["status"] == "conflict"
    assert {g["value"] for g in rows["масса"]["values"]} == {"120", "135"}


def test_a_missing_fact_is_reported_missing():
    rows = wr.aggregate_facts(_brief(), [])
    assert {r["status"] for r in rows} == {"missing"}


# ── the loop ─────────────────────────────────────────────────────────────────


SHOP = "https://shop.example/km-80-50-200"
MAKER = "https://hms.example/km-80-50-200"
PASSPORT = "https://hms.example/files/km-passport.pdf"
REJECTED = "https://spam.example/km"

TEXTS = {
    SHOP: "Насос КМ 80-50-200 купить. Подача 50 м3/ч. Доставка по России. " * 3,
    MAKER: "Насос КМ 80-50-200 — консольный моноблочный. Подача 50 м3/ч. Напор 50 м. " * 3,
    PASSPORT: "Паспорт насоса КМ 80-50-200. Масса агрегата 120 кг. Подача 50 м3/ч. " * 3,
    REJECTED: "КМ 80-50-200 масса 999 кг",
}

READINGS = {
    SHOP: {"kind": "marketplace", "facts": [("подача", "50", "Подача 50 м3/ч")], "links": []},
    MAKER: {
        "kind": "manufacturer",
        "facts": [
            ("подача", "50", "Подача 50 м3/ч"),
            ("напор", "50", "Напор 50 м"),
            ("масса", "95", "Масса насоса 95 кг"),
        ],  # invented: dropped
        "links": [PASSPORT],
    },
    PASSPORT: {
        "kind": "manufacturer",
        "facts": [("масса", "120", "Масса агрегата 120 кг")],
        "links": [],
    },
}


@pytest.fixture
def world(monkeypatch):
    searches: list[dict] = []
    fetched: list[str] = []

    async def fake_search(request):
        searches.append({"q": request.query, "domains": request.domains})
        urls = [REJECTED, SHOP, MAKER] if not request.domains else [MAKER]
        return ws.WebSearchResponse(
            query=request.query,
            provider="test",
            results=[ws.WebSearchResult(title=u, url=u) for u in urls],
        )

    async def fake_fetch(request):
        fetched.append(request.url)
        links = [ws.PageLink(url=PASSPORT, text="Паспорт PDF")] if request.url == MAKER else []
        text = TEXTS[request.url]
        return ws.WebFetchResponse(
            url=request.url,
            status=200,
            title=request.url,
            text=text,
            text_chars=len(text),
            links=links,
            read_status="ok",
        )

    async def fake_read(brief, page_text, page_url, title, links, missing):
        spec = READINGS[page_url]
        return wr.PageReading(
            kind=spec["kind"],
            kind_reason="тест",
            about_entity=True,
            facts=[wr.PageFact(name=n, value=v, unit=None, quote=q) for n, v, q in spec["facts"]],
            links=[wr.PageLinkPick(url=u, why="паспорт") for u in spec["links"]],
        )

    async def fake_registry():
        return {"hms.example"}, {"spam.example"}

    monkeypatch.setattr(ws, "execute_web_search", fake_search)
    monkeypatch.setattr(ws, "fetch_page", fake_fetch)
    monkeypatch.setattr(wr, "read_page", fake_read)
    monkeypatch.setattr(wr, "registry_domains", fake_registry)
    return searches, fetched


@pytest.mark.asyncio
async def test_rounds_follow_the_gaps_and_the_makers_links(world):
    searches, fetched = world
    result = await wr.deep_research(_brief(), wr.DeepLimits(max_rounds=3, pages_per_round=2))
    rows = {r["name"]: r for r in result["facts"]}
    assert rows["подача"]["status"] == "confirmed"  # maker and shop agree
    assert rows["напор"]["status"] == "single"
    assert rows["масса"]["values"][0]["value"] == "120"  # from the passport, not the invented 95
    assert rows["масса"]["status"] == "single"
    assert REJECTED not in fetched  # the owner rejected that site
    assert fetched[0] == MAKER  # the owner's approved site first
    assert PASSPORT in fetched  # followed from the maker's page
    # The gap was searched on the maker's own site.
    assert any(s["domains"] == ["hms.example"] for s in searches[1:])
    assert "fact_dropped:quote_not_on_page" in result["diagnostics"]
    kinds = {s["url"]: s.get("kind") for s in result["sources"]}
    assert kinds[MAKER] == "manufacturer" and kinds[SHOP] == "marketplace"
    assert result["sources"][0]["trusted"] is True


@pytest.mark.asyncio
async def test_a_vague_question_asks_instead_of_searching(monkeypatch, world):
    searches, _ = world

    async def fake_brief(question, context=None):
        return _brief(must_clarify=True, ambiguities=["какое исполнение"], queries=[]), []

    monkeypatch.setattr(wr, "make_brief", fake_brief)
    answer = await ws.execute_web_research(ws.WebResearchRequest(mode="deep", question="насос"))
    assert "must_clarify" in answer.diagnostics and answer.brief["ambiguities"]
    assert searches == []


@pytest.mark.asyncio
async def test_the_deep_answer_keeps_facts_in_the_model_history(world):
    import json

    from app.ai.agent_loop import _MAX_TOOL_RESULT_CHARS, _trim_tool_result

    answer = await ws.execute_web_research(
        ws.WebResearchRequest(mode="deep", brief=_brief().model_dump(), max_rounds=3)
    )
    envelope = json.dumps(
        {"version": 1, "status": "ok", "data": answer.model_dump()}, ensure_ascii=False
    )
    shown = json.loads(_trim_tool_result(envelope))
    assert len(json.dumps(shown, ensure_ascii=False)) <= _MAX_TOOL_RESULT_CHARS + 200
    assert {r["name"] for r in shown["data"]["facts"]} == {"подача", "напор", "масса"}


def test_unassigned_slot_uses_the_employees_model(monkeypatch):
    from app.ai import task_routing

    monkeypatch.setattr(task_routing, "_redis_get", lambda: {})
    assert wr.web_model_assigned() is False
    monkeypatch.setattr(
        task_routing, "_redis_get", lambda: {"web_research": {"models": ["qwen3_5_9b_ollama"]}}
    )
    assert wr.web_model_assigned() is True
    assert wr._preferred_model() is None


@pytest.mark.asyncio
async def test_a_single_source_value_gets_one_confirmation_round(world):
    searches, fetched = world
    brief = _brief(
        facts_needed=[
            {"name": "подача", "unit": "м3/ч", "why": None},
            {"name": "напор", "unit": "м", "why": None},
        ]
    )
    result = await wr.deep_research(brief, wr.DeepLimits(max_rounds=3, pages_per_round=1))
    rounds = result["rounds"]
    # Round 1 (maker) gives both; напор is single → one more round, then stop.
    assert len(rounds) == 2
    assert any("напор" in q for q in rounds[1]["queries"])
