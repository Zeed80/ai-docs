from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.web_search import WebSearchResponse, WebSearchResult
from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, UserRole
from app.main import app

_AGENT_SERVICE_USER = UserInfo(
    sub="agent-service",
    email="agent@internal",
    name="AI Agent (Света)",
    preferred_username="agent",
    roles=[UserRole.admin],
    groups=["agents"],
)


@pytest.mark.asyncio
async def test_agent_service_account_cannot_decide_promotion(client: AsyncClient):
    """The agent authenticates as admin but must not approve its own proposals."""
    proposed = await client.post(
        "/api/memory/promotions",
        json={
            "title": "Проверенный источник АКМЕ",
            "summary": "Поставщик АКМЕ публикует каталог крепежа на официальном сайте.",
            "metadata": {"url": "https://example.com/acme"},
        },
    )
    assert proposed.status_code == 200
    proposal_id = proposed.json()["id"]

    app.dependency_overrides[get_current_user] = lambda: _AGENT_SERVICE_USER
    try:
        denied = await client.post(
            f"/api/memory/promotions/{proposal_id}/decide",
            json={"approved": True},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_memory_promotion_from_session_fact_can_be_approved(client: AsyncClient):
    chat = await client.post(
        "/api/memory/chat-turn",
        json={
            "user_text": "Поставщик АКМЕ прислал новый каталог",
            "assistant_text": "Каталог АКМЕ относится к крепежу.",
            "session_id": "promotion-session",
        },
    )
    assert chat.status_code == 200
    source_id = chat.json()["id"]

    proposed = await client.post(
        "/api/memory/promotions",
        json={
            "source_fact_id": source_id,
            "metadata": {"reason": "verified by user"},
        },
    )
    assert proposed.status_code == 200
    proposal = proposed.json()
    assert proposal["scope"] == "project"
    assert proposal["kind"] == "proposed_fact"
    assert proposal["metadata"]["promotion_status"] == "pending"
    assert proposal["metadata"]["source_fact_id"] == source_id

    listed = await client.get("/api/memory/promotions", params={"status": "pending"})
    assert listed.status_code == 200
    assert any(item["id"] == proposal["id"] for item in listed.json())

    decided = await client.post(
        f"/api/memory/promotions/{proposal['id']}/decide",
        json={"approved": True, "decided_by": "tester", "comment": "ok"},
    )
    assert decided.status_code == 200
    data = decided.json()
    assert data["kind"] == "verified_fact"
    assert data["pinned"] is True
    assert data["metadata"]["promotion_status"] == "approved"


@pytest.mark.asyncio
async def test_memory_promotion_list_excludes_superseded_duplicates(
    client: AsyncClient, db_session: AsyncSession
):
    """Ф6 (AGENT_AUTONOMY_ROADMAP.md): found while wiring the idle-reflection
    duplicate-proposal consolidation task — list_memory_promotions never
    checked MemoryFact.status, so a proposal the consolidation job marks
    superseded (an exact duplicate collapsed onto a newer one) would still
    show up in the review queue as if pending, letting a human act on a
    stale duplicate instead of the kept one."""
    from sqlalchemy import select

    from app.db.models import MemoryFact

    proposed = await client.post(
        "/api/memory/promotions",
        json={
            "title": "Дубль для теста Ф6",
            "summary": "Будет помечен superseded вручную, как это делает idle-reflection",
            "metadata": {"reason": "test"},
        },
    )
    assert proposed.status_code == 200
    proposal_id = proposed.json()["id"]

    fact = (
        await db_session.execute(select(MemoryFact).where(MemoryFact.id == proposal_id))
    ).scalar_one()
    fact.status = "superseded"
    await db_session.commit()

    listed = await client.get("/api/memory/promotions", params={"status": "pending"})
    assert listed.status_code == 200
    assert not any(item["id"] == proposal_id for item in listed.json())


@pytest.mark.asyncio
async def test_memory_promotion_evaluate_reports_quality_checks(client: AsyncClient):
    proposed = await client.post(
        "/api/memory/promotions",
        json={
            "title": "Проверенный источник АКМЕ",
            "summary": "Поставщик АКМЕ публикует каталог крепежа на официальном сайте.",
            "metadata": {"url": "https://example.com/acme"},
        },
    )
    assert proposed.status_code == 200
    proposal = proposed.json()

    evaluation = await client.post(f"/api/memory/promotions/{proposal['id']}/evaluate")
    assert evaluation.status_code == 200
    data = evaluation.json()
    assert data["passed"] is True
    assert {check["name"] for check in data["checks"]} >= {
        "provenance",
        "summary_length",
        "confidence",
        "duplicate_title",
    }

    approved = await client.post(
        f"/api/memory/promotions/{proposal['id']}/decide",
        json={"approved": True, "decided_by": "tester"},
    )
    assert approved.status_code == 200

    duplicate = await client.post(
        "/api/memory/promotions",
        json={
            "title": "Проверенный источник АКМЕ",
            "summary": "Повторное утверждение того же источника АКМЕ.",
            "metadata": {"url": "https://example.com/acme"},
        },
    )
    duplicate_eval = await client.post(f"/api/memory/promotions/{duplicate.json()['id']}/evaluate")
    assert duplicate_eval.status_code == 200
    assert duplicate_eval.json()["passed"] is False
    assert "duplicate_title" in duplicate_eval.json()["diagnostics"]


@pytest.mark.asyncio
async def test_web_source_registry_propose_list_and_approve(client: AsyncClient):
    proposed = await client.post(
        "/api/memory/sources/propose",
        json={
            "title": "АКМЕ каталог крепежа",
            "url": "https://example.com/acme/catalog",
            "supplier_name": "АКМЕ",
            "source_type": "supplier_catalog",
            "rationale": "Нужен источник для сверки цен",
            "domains": ["example.com"],
        },
    )
    assert proposed.status_code == 200
    source = proposed.json()
    assert source["kind"] == "web_source"
    assert source["metadata"]["source_status"] == "proposed"

    listed = await client.get("/api/memory/sources", params={"status": "proposed"})
    assert listed.status_code == 200
    assert any(item["id"] == source["id"] for item in listed.json())

    decided = await client.post(
        f"/api/memory/sources/{source['id']}/decide",
        json={"approved": True, "decided_by": "tester"},
    )
    assert decided.status_code == 200
    data = decided.json()
    assert data["pinned"] is True
    assert data["metadata"]["source_status"] == "approved"


@pytest.mark.asyncio
async def test_web_source_discovery_creates_reviewable_proposals(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    existing = await client.post(
        "/api/memory/sources/propose",
        json={
            "title": "Existing АКМЕ",
            "url": "https://example.com/acme/catalog",
            "supplier_name": "АКМЕ",
            "source_type": "supplier_catalog",
        },
    )
    assert existing.status_code == 200

    async def fake_search(payload):
        assert payload.query == "АКМЕ официальный каталог"
        assert payload.intent == "source_discovery"
        return WebSearchResponse(
            query=payload.query,
            provider="custom",
            results=[
                WebSearchResult(
                    title="Existing АКМЕ",
                    url="https://example.com/acme/catalog",
                    snippet="Already known",
                ),
                WebSearchResult(
                    title="New АКМЕ catalog",
                    url="https://catalog.example/acme",
                    snippet="Official catalog page",
                ),
            ],
        )

    monkeypatch.setattr("app.api.memory.execute_web_search", fake_search)

    discovered = await client.post(
        "/api/memory/sources/discover",
        json={
            "supplier_name": "АКМЕ",
            "source_type": "supplier_catalog",
            "limit": 5,
        },
    )

    assert discovered.status_code == 200
    data = discovered.json()
    assert data["query"] == "АКМЕ официальный каталог"
    assert data["provider"] == "custom"
    assert data["skipped_existing"] == 1
    assert len(data["proposed"]) == 1
    proposal = data["proposed"][0]
    assert proposal["kind"] == "web_source"
    assert proposal["source"] == "web_source_discovery"
    assert proposal["metadata"]["source_status"] == "proposed"
    assert proposal["metadata"]["url"] == "https://catalog.example/acme"
    assert proposal["metadata"]["discovery_provider"] == "custom"
