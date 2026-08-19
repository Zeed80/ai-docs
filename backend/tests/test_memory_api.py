"""Tests for Memory API — search, chat-turn, pin, prune."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.db.models import MemoryFact


# ── Search ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skip(reason="Memory search uses pg_trgm/@@ — not supported by SQLite test DB")
async def test_memory_search_empty(client: AsyncClient):
    resp = await client.post("/api/memory/search", json={
        "query": "счёт от АКМЕ",
        "limit": 10,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "hits" in data
    assert "total" in data
    assert "query" in data
    assert isinstance(data["hits"], list)


@pytest.mark.asyncio
@pytest.mark.skip(reason="Memory search uses pg_trgm/@@ — not supported by SQLite test DB")
async def test_memory_search_returns_structure(client: AsyncClient):
    resp = await client.post("/api/memory/search", json={
        "query": "аномалия поставщик",
        "limit": 5,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["retrieval_mode"] == "auto_hybrid"
    assert "coverage" in data
    assert "total_available" in data


# ── Chat turn ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_chat_turn(client: AsyncClient):
    resp = await client.post("/api/memory/chat-turn", json={
        "user_text": "Покажи счета от ООО АКМЕ",
        "assistant_text": "Нашел 3 счёта от ООО АКМЕ на сумму 50 000 руб.",
        "session_id": "test-session-001",
        "scope": "project",
        "confidence": 0.8,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["scope"] == "session"
    assert data["kind"] == "chat_turn"
    assert data["metadata"]["requested_scope"] == "project"
    assert data["metadata"]["scope_policy"] == "chat_turn_demoted_to_session"


@pytest.mark.asyncio
async def test_store_chat_turn_project_scope_requires_trusted_metadata(client: AsyncClient):
    resp = await client.post("/api/memory/chat-turn", json={
        "user_text": "Это проверенный факт проекта",
        "assistant_text": "Факт можно использовать повторно.",
        "scope": "project",
        "metadata": {"trusted": True},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["scope"] == "project"
    assert data["kind"] == "chat_turn"


@pytest.mark.asyncio
async def test_store_chat_turn_minimal(client: AsyncClient):
    resp = await client.post("/api/memory/chat-turn", json={
        "user_text": "Привет",
        "assistant_text": "Здравствуйте!",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data


# ── Pin ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pin_memory_fact(client: AsyncClient):
    resp = await client.post("/api/memory/pin", json={
        "title": "Договор с ООО АКМЕ",
        "summary": "Годовой контракт на поставку болтов, сумма 2 млн руб.",
        "scope": "project",
        "kind": "pinned_fact",
        "confidence": 1.0,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["pinned"] is True
    assert data["title"] == "Договор с ООО АКМЕ"


@pytest.mark.asyncio
@pytest.mark.skip(reason="Memory search uses pg_trgm/@@ — not supported by SQLite test DB")
async def test_pin_appears_in_search(client: AsyncClient):
    await client.post("/api/memory/pin", json={
        "title": "Важный факт для поиска",
        "summary": "Тестовый pinned факт для проверки поиска памяти",
        "scope": "project",
    })

    resp = await client.post("/api/memory/search", json={
        "query": "pinned факт",
        "limit": 20,
    })
    assert resp.status_code == 200
    assert isinstance(resp.json()["hits"], list)


# ── Prune ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_memory_no_scope(client: AsyncClient):
    resp = await client.post("/api/memory/prune", json={
        "scope": "session",
        "max_age_days": 1,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "deleted" in data or "deleted_count" in data


# ── Verified WorkOrder learning ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_learned_memory_is_owner_scoped_and_can_be_superseded(
    client: AsyncClient, db_session
):
    now = datetime.now(UTC)
    owned = MemoryFact(
        scope="owner:dev-user",
        kind="work_order_lesson",
        title="Проверенный порядок",
        summary="Сначала проверить входные данные",
        source="work_order",
        confidence=1.0,
        last_verified_at=now,
        valid_from=now,
        expires_at=now + timedelta(days=30),
        status="active",
        provenance={"work_order_id": "owned"},
    )
    foreign = MemoryFact(
        scope="owner:another-user",
        kind="work_order_lesson",
        title="Чужой порядок",
        summary="Не должен быть виден",
        source="work_order",
        confidence=1.0,
        status="active",
        provenance={"work_order_id": "foreign"},
    )
    db_session.add_all([owned, foreign])
    await db_session.commit()

    listed = await client.get("/api/memory/learned")
    assert listed.status_code == 200
    rows = listed.json()
    assert [row["id"] for row in rows] == [str(owned.id)]

    replacement = await client.post(
        f"/api/memory/learned/{owned.id}/supersede",
        json={
            "summary": "Сначала проверить входные данные и полномочия",
            "reason": "Уточнение после проверки",
            "valid_days": 90,
        },
    )
    assert replacement.status_code == 200
    replacement_data = replacement.json()
    assert replacement_data["status"] == "active"
    assert replacement_data["provenance"]["supersedes"] == str(owned.id)

    await db_session.refresh(owned)
    assert owned.status == "superseded"
    assert str(owned.superseded_by_id) == replacement_data["id"]
