"""Tests for Collections API."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import Document, DocumentStatus


@pytest.fixture
async def sample_doc(db_session):
    doc = Document(
        file_name="coll-test.pdf", file_hash="coll_hash_1", file_size=100,
        mime_type="application/pdf", storage_path="t/coll.pdf",
        status=DocumentStatus.approved,
    )
    db_session.add(doc)
    await db_session.commit()
    return doc


@pytest.mark.asyncio
async def test_collection_lifecycle(client: AsyncClient, sample_doc):
    # Create
    resp = await client.post("/api/collections", json={
        "name": "Закупка Q1",
        "description": "Документы по закупке за Q1",
    })
    assert resp.status_code == 200
    coll = resp.json()
    coll_id = coll["id"]
    assert coll["name"] == "Закупка Q1"
    assert coll["is_closed"] is False

    # Add item
    resp = await client.post(f"/api/collections/{coll_id}/items", json={
        "entity_type": "document",
        "entity_id": str(sample_doc.id),
        "note": "Основной счёт",
    })
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1

    # Duplicate add should fail
    resp = await client.post(f"/api/collections/{coll_id}/items", json={
        "entity_type": "document",
        "entity_id": str(sample_doc.id),
    })
    assert resp.status_code == 400

    # Get
    resp = await client.get(f"/api/collections/{coll_id}")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1

    # Summarize
    resp = await client.post(f"/api/collections/{coll_id}/summarize")
    assert resp.status_code == 200
    data = resp.json()
    assert data["item_count"] == 1
    assert "документов" in data["summary"]

    # Timeline
    resp = await client.get(f"/api/collections/{coll_id}/timeline")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1  # at least the item_added event

    # Close
    resp = await client.post(f"/api/collections/{coll_id}/close")
    assert resp.status_code == 200
    assert resp.json()["is_closed"] is True
    assert resp.json()["closure_summary"] is not None

    # Can't add to closed collection
    resp = await client.post(f"/api/collections/{coll_id}/items", json={
        "entity_type": "document",
        "entity_id": str(uuid.uuid4()),
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_collections(client: AsyncClient):
    # Create two collections
    await client.post("/api/collections", json={"name": "A"})
    await client.post("/api/collections", json={"name": "B"})

    resp = await client.get("/api/collections")
    assert resp.status_code == 200
    assert len(resp.json()) >= 2
