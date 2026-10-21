"""Memory recall: facts by the words of a question, and a reranker that counts.

Measured 2026-10-21 on 24 live questions: no fact, web source or lesson came
into a turn (they were matched by the whole question as a substring), and the
reranker changed no order (its 0..1 verdict lost to fused scores near 1.0).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.db.models import MemoryFact


def _fact(title: str, summary: str) -> MemoryFact:
    return MemoryFact(
        scope="owner:dev-user",
        kind="verified_fact",
        title=title,
        summary=summary,
        source="test",
        confidence=1.0,
        status="active",
        provenance={},
    )


@pytest.mark.asyncio
async def test_a_fact_is_found_by_the_words_of_a_question(client: AsyncClient, db_session):
    fact = _fact(
        "Местоположение предприятия — Подольск",
        "Предприятие находится в Подольске, Московская область.",
    )
    db_session.add(fact)
    await db_session.commit()

    resp = await client.post(
        "/api/memory/search",
        json={"query": "Где находится наше предприятие?", "limit": 20, "rerank": False},
    )
    assert resp.status_code == 200
    assert str(fact.id) in [hit["id"] for hit in resp.json()["hits"]]


@pytest.mark.asyncio
async def test_the_reranker_orders_its_candidates(client: AsyncClient, db_session, monkeypatch):
    facts = [
        _fact("Порядок закупки фрез", "Фрезы заказываем у поставщика А."),
        _fact("Порядок оплаты фрез", "Фрезы оплачиваем по счёту поставщика."),
        _fact("Фрезы для паза", "Фрезы ласточкин хвост берём у СИЭНСИЭМ."),
    ]
    db_session.add_all(facts)
    await db_session.commit()

    from app.ai.router import AIRouter
    from app.ai.schemas import AIResponse
    from app.api import memory

    async def _run(self, request):
        docs = request.metadata["documents"]
        scores = [0.95 if "ласточкин" in (doc or "") else 0.05 for doc in docs]
        return AIResponse(task=request.task, provider="ollama", model="fake", scores=scores)

    monkeypatch.setattr(memory, "_reranker_model_key", lambda: "fake_reranker")
    monkeypatch.setattr(AIRouter, "run", _run)

    resp = await client.post(
        "/api/memory/search",
        json={"query": "фрезы", "limit": 5, "rerank": True},
    )
    assert resp.status_code == 200
    hits = resp.json()["hits"]
    assert hits[0]["id"] == str(facts[2].id)
    assert hits[0]["rerank_score"] == pytest.approx(0.95)
