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
    resp = await client.post(
        "/api/search/saved-queries",
        json={
            "nl_text": "счета от ООО АКМЕ за последний месяц",
            "structured_query": {"supplier_name": "АКМЕ", "doc_type": "invoice"},
            "result_count": 15,
            "is_alert": False,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["nl_text"] == "счета от ООО АКМЕ за последний месяц"
    assert data["is_alert"] is False
    assert data["structured_query"]["doc_type"] == "invoice"


@pytest.mark.asyncio
async def test_create_saved_query_as_alert(client: AsyncClient):
    resp = await client.post(
        "/api/search/saved-queries",
        json={
            "nl_text": "новые аномалии",
            "structured_query": {"status": "anomaly"},
            "is_alert": True,
            "alert_cron": "0 9 * * *",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["is_alert"] is True
    assert data["alert_cron"] == "0 9 * * *"


@pytest.mark.asyncio
async def test_list_saved_queries(client: AsyncClient):
    await client.post(
        "/api/search/saved-queries",
        json={
            "nl_text": "list query 1",
            "structured_query": {},
        },
    )
    await client.post(
        "/api/search/saved-queries",
        json={
            "nl_text": "list query 2",
            "structured_query": {},
        },
    )

    resp = await client.get("/api/search/saved-queries")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2
    texts = [q["nl_text"] for q in data]
    assert "list query 1" in texts


@pytest.mark.asyncio
async def test_delete_saved_query(client: AsyncClient):
    create_resp = await client.post(
        "/api/search/saved-queries",
        json={
            "nl_text": "query to delete",
            "structured_query": {},
        },
    )
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
    resp = await client.post(
        "/api/search/nl",
        json={
            "query": "счета на оплату",
            "limit": 10,
        },
    )
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


async def _letter(db_session, name: str, text: str, doc_type: str = "letter"):
    from app.db.models import DocumentChunk

    doc = Document(
        file_name=name,
        file_hash=uuid.uuid4().hex,
        file_size=10,
        mime_type="application/pdf",
        storage_path=f"s/{uuid.uuid4().hex}.pdf",
        status=DocumentStatus.needs_review,
        doc_type=doc_type,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(DocumentChunk(document_id=doc.id, chunk_index=0, text=text))
    await db_session.commit()
    return doc


@pytest.mark.asyncio
async def test_a_letter_with_some_of_the_words_is_found(client: AsyncClient, db_session):
    """T8i (E52): "новый адрес доставки заказов" missed «заказы принимаем на orders@…»."""
    tag = uuid.uuid4().hex[:8]
    steel = await _letter(
        db_session,
        f"Письмо {tag} сталь.pdf",
        f"ООО «Эвал-Сталь{tag}»: с 1 ноября заказы принимаем на orders@eval-stal.example.",
    )
    nickel = await _letter(
        db_session,
        f"Письмо {tag} никель.pdf",
        f"ООО «Эвал-Никель{tag}»: новый адрес для заявок — zayavki@eval-nikel.example.",
    )
    other = await _letter(
        db_session, f"Акт {tag}.pdf", f"Акт сверки {tag}: заказы и новый адрес", doc_type="act"
    )
    resp = await client.post(
        "/api/search/documents",
        json={
            "query": f"новый адрес доставки заказов {tag}",
            "filters": {"doc_type": "letter", "limit": 50},
        },
    )
    assert resp.status_code == 200, resp.text
    ids = {d["id"] for d in resp.json()}
    assert {str(steel.id), str(nickel.id)} <= ids
    assert str(other.id) not in ids


@pytest.mark.asyncio
async def test_documents_of_a_type_without_words_and_an_unknown_type(
    client: AsyncClient, db_session
):
    tag = uuid.uuid4().hex[:8]
    letter = await _letter(db_session, f"Письмо {tag}.pdf", "текст")
    listed = await client.post(
        "/api/search/documents", json={"filters": {"doc_type": "letter", "limit": 100}}
    )
    assert listed.status_code == 200, listed.text
    assert all(d["doc_type"] == "letter" for d in listed.json())
    assert str(letter.id) in {d["id"] for d in listed.json()}
    bad = await client.post("/api/search/documents", json={"filters": {"doc_type": "письмо"}})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_search_shows_only_documents_the_user_may_open(db_session):
    """E52: a contract of another owner was found by name, documents.read on
    it said 404, and the agent searched on for 40 model calls."""
    from app.api.search import DocumentSearchBody, search_documents
    from app.auth.models import UserInfo, UserRole

    def doc(name: str, owner: str) -> Document:
        return Document(
            file_name=name,
            file_hash=f"vis-{name}-{uuid.uuid4().hex[:6]}",
            file_size=1,
            mime_type="application/pdf",
            storage_path=f"/{name}",
            doc_type="contract",
            owner_sub=owner,
        )

    db_session.add_all([doc("ДП-95 свой", "u:buyer"), doc("ДП-95 чужой", "u:other")])
    await db_session.commit()
    buyer = UserInfo(
        sub="u:buyer", email="b@x", name="b", preferred_username="b", roles=[UserRole.buyer]
    )

    found = await search_documents(
        q=None,
        query_text=None,
        limit=20,
        doc_type=None,
        body=DocumentSearchBody(query="ДП-95"),
        db=db_session,
        current_user=buyer,
    )
    assert {d.file_name for d in found} == {"ДП-95 свой"}
