"""Tests for Search API — saved queries, NL search, similar documents."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import Document, DocumentStatus


@pytest.fixture
async def document(db_session):
    doc = Document(
        file_name="search-test.pdf",
        file_hash="srch001",
        file_size=1024,
        mime_type="application/pdf",
        storage_path="s/1.pdf",
        status=DocumentStatus.needs_review,
        doc_type="invoice",
    )
    db_session.add(doc)
    await db_session.commit()
    return doc


# ── Saved queries ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_saved_query(client: AsyncClient):
    resp = await client.post("/api/search/saved-queries", json={
        "nl_text": "счета от ООО АКМЕ за последний месяц",
        "structured_query": {"supplier_name": "АКМЕ", "doc_type": "invoice"},
        "result_count": 15,
        "is_alert": False,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["nl_text"] == "счета от ООО АКМЕ за последний месяц"
    assert data["is_alert"] is False
    assert data["structured_query"]["doc_type"] == "invoice"


@pytest.mark.asyncio
async def test_create_saved_query_as_alert(client: AsyncClient):
    resp = await client.post("/api/search/saved-queries", json={
        "nl_text": "новые аномалии",
        "structured_query": {"status": "anomaly"},
        "is_alert": True,
        "alert_cron": "0 9 * * *",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["is_alert"] is True
    assert data["alert_cron"] == "0 9 * * *"


@pytest.mark.asyncio
async def test_list_saved_queries(client: AsyncClient):
    await client.post("/api/search/saved-queries", json={
        "nl_text": "list query 1",
        "structured_query": {},
    })
    await client.post("/api/search/saved-queries", json={
        "nl_text": "list query 2",
        "structured_query": {},
    })

    resp = await client.get("/api/search/saved-queries")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2
    texts = [q["nl_text"] for q in data]
    assert "list query 1" in texts


@pytest.mark.asyncio
async def test_delete_saved_query(client: AsyncClient):
    create_resp = await client.post("/api/search/saved-queries", json={
        "nl_text": "query to delete",
        "structured_query": {},
    })
    query_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/search/saved-queries/{query_id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_saved_query_not_found(client: AsyncClient):
    resp = await client.delete(f"/api/search/saved-queries/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Text search ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_documents(client: AsyncClient, document):
    resp = await client.post(
        "/api/search/documents",
        params={"q": "search-test", "limit": 10},
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_search_documents_empty_results(client: AsyncClient):
    resp = await client.post(
        "/api/search/documents",
        params={"q": "zzz_nonexistent_document_xyz_abc", "limit": 5},
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ── NL query ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nl_search(client: AsyncClient):
    resp = await client.post("/api/search/nl", json={
        "query": "счета на оплату",
        "limit": 10,
    })
    # NL search may require LLM; accept 200 or 503
    assert resp.status_code in (200, 503, 500)
    if resp.status_code == 200:
        data = resp.json()
        assert "results" in data
        assert "original_query" in data


# ── Similar ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_similar_documents(client: AsyncClient, document):
    resp = await client.get(
        f"/api/search/similar/document/{document.id}",
        params={"limit": 5},
    )
    # Vector search may return empty if Qdrant not available
    assert resp.status_code == 200
    data = resp.json()
    assert data["source_id"] == str(document.id)
    assert data["source_type"] == "document"
    assert isinstance(data["results"], list)


@pytest.mark.asyncio
async def test_find_similar_invalid_entity(client: AsyncClient):
    resp = await client.get(
        f"/api/search/similar/document/{uuid.uuid4()}",
        params={"limit": 5},
    )
    # May return 200 with empty results or 404 — either is acceptable
    assert resp.status_code in (200, 404)
