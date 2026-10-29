"""W5: what the answer cites is what the turn read."""

from __future__ import annotations

import pytest

from app.ai.audit import AuditCode
from app.ai.web_citations import (
    SourceRegister,
    check_answer,
    cited_in,
    mark_unconfirmed,
    number_sources,
    sources_block,
)

MAKER = "https://hms.example/km-100-80-160"
SHOP = "https://shop.example/km"
WALL = "https://wall.example/km"
MAKER_TEXT = (
    "Насос КМ 100-80-160. Подача (номин.), м³/ч | 100. Напор (номин.), м | 32. Мощность, кВт | 15."
)


def _envelope(data: dict) -> dict:
    return {"version": 1, "status": "ok", "data": data}


def _register() -> SourceRegister:
    register = SourceRegister()
    number_sources(
        register,
        "search",
        {"action": "research"},
        _envelope(
            {
                "sources": [
                    {
                        "n": 1,
                        "url": MAKER,
                        "title": "Ливгидромаш",
                        "read_status": "ok",
                        "source_ref": "r1",
                    },
                    {
                        "n": 2,
                        "url": SHOP,
                        "title": "Магазин",
                        "read_status": "ok",
                        "source_ref": "r2",
                    },
                    {"n": 3, "url": WALL, "read_status": "blocked"},
                ]
            }
        ),
    )
    return register


# ── numbering ────────────────────────────────────────────────────────────────


def test_numbers_run_through_the_turn_and_a_page_keeps_its_number():
    register = _register()
    browse = _envelope({"url": "https://other.example/p", "read_status": "ok", "source_ref": "r4"})
    number_sources(register, "search", {"action": "browse"}, browse)
    assert browse["data"]["n"] == 4 and browse["data"]["cite_as"] == "[4]"
    # The same page again (tracking parameters differ): the same number.
    again = _envelope({"url": MAKER + "?utm_source=ya", "read_status": "ok"})
    number_sources(register, "search", {"action": "browse"}, again)
    assert again["data"]["n"] == 1
    # read_source names its page by ref.
    more = _envelope({"source_ref": "r2", "url": SHOP, "text": "…"})
    number_sources(register, "search", {"action": "read_source"}, more)
    assert more["data"]["cite_as"] == "[2]"


def test_a_second_research_renumbers_its_sources_and_facts():
    register = _register()
    second = _envelope(
        {
            "sources": [
                {"n": 1, "url": "https://new.example/a", "read_status": "ok"},
                {"n": 2, "url": SHOP, "read_status": "ok"},
                {"n": 3, "url": "https://new.example/b", "read_status": "ok", "duplicate_of": 1},
            ],
            "facts": [{"name": "напор", "values": [{"value": "32", "sources": [1, 2]}]}],
        }
    )
    number_sources(register, "search", {"action": "research"}, second)
    assert [s["n"] for s in second["data"]["sources"]] == [4, 2, 5]
    assert second["data"]["facts"][0]["values"][0]["sources"] == [4, 2]
    assert second["data"]["sources"][2]["duplicate_of"] == 4


def test_a_browser_read_is_numbered_with_its_text():
    register = SourceRegister()
    result = {"action_id": "a1", "result": {"url": MAKER, "text": MAKER_TEXT}}
    number_sources(register, "computer_use", {"action": "desktop_read"}, result)
    assert result["result"]["n"] == 1 and register.get(1)["text"] == MAKER_TEXT


# ── checking the answer ──────────────────────────────────────────────────────


def _codes(answer: str, texts=None) -> list[str]:
    return [i.code.value for i in check_answer(answer, _register(), texts or {1: MAKER_TEXT})]


def test_a_correct_answer_passes():
    answer = "КМ 100-80-160: подача 100 м³/ч [1], напор 32 м [1], мощность 15 кВт [1]."
    assert _codes(answer) == []


def test_a_number_not_on_the_cited_page():
    issues = check_answer("Масса 165 кг [1].", _register(), {1: MAKER_TEXT})
    assert [i.code for i in issues] == [AuditCode.VALUE_NOT_IN_SOURCE]
    assert issues[0].context["value"] == 165


def test_a_citation_of_nothing_read_and_a_foreign_url():
    assert _codes("Напор 32 м [9].") == ["cited_source_not_read"]
    assert "cited_source_not_read" in _codes("См. https://elsewhere.example/km — напор 32 м [1].")


def test_a_blocked_page_is_no_confirmation():
    assert "blocked_source_used" in _codes("Подача 100 м³/ч [3].")


def test_uncited_numbers_are_advisory_only_when_nothing_is_cited():
    issues = check_answer("Подача насоса 100 м³/ч, напор 32 м.", _register(), {})
    assert [i.code for i in issues] == [AuditCode.UNCITED_WEB_CLAIM]
    assert issues[0].severity == "advisory"


def test_years_and_unknown_texts_are_not_flagged():
    assert _codes("Каталог 2024 года [1]: напор 32 м.") == []
    # The page text is not at hand: unknown, not wrong.
    assert _codes("Масса 165 кг [2].", texts={}) == []


def test_a_table_row_is_one_claim():
    answer = "| Напор | 32 м | [1] |\n| Масса | 165 кг | [1] |"
    assert _codes(answer) == ["value_not_in_source"]


def test_sources_block_and_marks():
    register = _register()
    answer = "Напор 32 м [1], подача 100 м³/ч [1][2]."
    assert cited_in(answer) == [1, 2]
    block = sources_block(answer, register)
    assert block.startswith("Источники:") and MAKER in block and SHOP in block
    issues = check_answer("Масса 165 кг [1].", register, {1: MAKER_TEXT})
    marked = mark_unconfirmed("Масса 165 кг [1].", issues)
    assert "⚠ Не подтверждено источниками" in marked and "165" in marked


# ── the orchestrator repairs once, then says what is left ────────────────────


class _Executor:
    def __init__(self, register):
        self.web_sources = register


@pytest.mark.asyncio
async def test_the_answer_is_repaired_once_and_sources_listed(monkeypatch):
    from app.ai import orchestrator as orch
    from app.ai.agent_config import BuiltinAgentConfig
    from app.ai.orchestrator import AgentOrchestrator, AuditReport

    sent: list[dict] = []

    async def send(event):
        sent.append(event)

    agent = AgentOrchestrator(send)
    agent._executor = _Executor(_register())
    agent._trace.text_chunks = ["Напор 32 м [1], масса 165 кг [1]."]

    async def texts(self, register, numbers):
        return {1: MAKER_TEXT}

    monkeypatch.setattr(AgentOrchestrator, "_web_source_texts", texts)
    prompts: list[str] = []

    async def fake_revise(response, task, issues, generate):
        prompts.append("\n".join(issues))
        return "Напор 32 м [1]; массу на странице производителя не нашёл."

    monkeypatch.setattr("app.ai.self_refine.revise_with_issues", fake_revise)
    plan = type("P", (), {"goal": "характеристики КМ 100-80-160"})()
    await agent._settle_web_citations(plan, BuiltinAgentConfig(), AuditReport(passed=True))

    final = agent._trace.final_text
    assert "165" not in final and "массу на странице производителя не нашёл" in final
    assert "Источники:" in final and MAKER in final
    assert "165" in prompts[0]  # the repair was told what was wrong
    kinds = [e["type"] for e in sent]
    assert kinds == ["web.sources", "answer.revised"]
    assert sent[0]["sources"][0]["url"] == MAKER
    assert orch  # module imported for monkeypatch targets


@pytest.mark.asyncio
async def test_what_cannot_be_repaired_is_marked(monkeypatch):
    from app.ai.agent_config import BuiltinAgentConfig
    from app.ai.orchestrator import AgentOrchestrator, AuditReport

    async def send(event):
        pass

    agent = AgentOrchestrator(send)
    agent._executor = _Executor(_register())
    agent._trace.text_chunks = ["Масса 165 кг [1]."]

    async def texts(self, register, numbers):
        return {1: MAKER_TEXT}

    async def same(response, task, issues, generate):
        return response

    monkeypatch.setattr(AgentOrchestrator, "_web_source_texts", texts)
    monkeypatch.setattr("app.ai.self_refine.revise_with_issues", same)
    plan = type("P", (), {"goal": "масса"})()
    await agent._settle_web_citations(plan, BuiltinAgentConfig(), AuditReport(passed=True))
    assert "⚠ Не подтверждено источниками" in agent._trace.final_text


def test_the_revise_prompt_shows_the_previous_answer():
    from app.ai.self_refine import _REVISE_PROMPT

    prompt = _REVISE_PROMPT.format(task="задача", response="ПРЕЖНИЙ ОТВЕТ", issues="- ошибка")
    assert "ПРЕЖНИЙ ОТВЕТ" in prompt
