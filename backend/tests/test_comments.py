"""Tests for Comments API — threaded comments on entities."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import Comment, Document, DocumentStatus


@pytest.fixture
async def source_doc(db_session):
    doc = Document(
        file_name="comments-test.pdf",
        file_hash="cmt001",
        file_size=512,
        mime_type="application/pdf",
        storage_path="c/1.pdf",
        status=DocumentStatus.approved,
    )
    db_session.add(doc)
    await db_session.commit()
    return doc


@pytest.fixture
async def existing_comment(db_session, source_doc):
    c = Comment(
        entity_type="document",
        entity_id=source_doc.id,
        user_id="dev-user",
        body="Первый комментарий",
    )
    db_session.add(c)
    await db_session.commit()
    return c


# ── List ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_comments_empty(client: AsyncClient, source_doc):
    resp = await client.get(
        "/api/comments",
        params={"entity_type": "document", "entity_id": str(source_doc.id)},
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_list_comments(client: AsyncClient, source_doc, existing_comment):
    resp = await client.get(
        "/api/comments",
        params={"entity_type": "document", "entity_id": str(source_doc.id)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    texts = [c.get("text") or c.get("body") for c in data]
    assert any("Первый комментарий" in (t or "") for t in texts)


# ── Create ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_comment(client: AsyncClient, source_doc):
    resp = await client.post("/api/comments", json={
        "entity_type": "document",
        "entity_id": str(source_doc.id),
        "text": "Проверьте раздел 3.2",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    text_val = data.get("text") or data.get("body", "")
    assert "3.2" in text_val
    assert data["entity_type"] == "document"


@pytest.mark.asyncio
async def test_create_reply_comment(client: AsyncClient, source_doc, existing_comment):
    resp = await client.post("/api/comments", json={
        "entity_type": "document",
        "entity_id": str(source_doc.id),
        "text": "Согласен с замечанием",
        "parent_id": str(existing_comment.id),
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["parent_id"] == str(existing_comment.id)


# ── Update ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_comment(client: AsyncClient, existing_comment):
    resp = await client.patch(f"/api/comments/{existing_comment.id}", json={
        "text": "Исправленный текст комментария",
    })
    assert resp.status_code == 200
    data = resp.json()
    text_val = data.get("text") or data.get("body", "")
    assert "Исправленный" in text_val


@pytest.mark.asyncio
async def test_update_comment_not_found(client: AsyncClient):
    resp = await client.patch(f"/api/comments/{uuid.uuid4()}", json={
        "text": "Нет такого комментария",
    })
    assert resp.status_code in (404, 403)


# ── Delete ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_comment(client: AsyncClient, source_doc):
    create_resp = await client.post("/api/comments", json={
        "entity_type": "document",
        "entity_id": str(source_doc.id),
        "text": "Временный комментарий",
    })
    comment_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/comments/{comment_id}")
    assert resp.status_code in (200, 204)
