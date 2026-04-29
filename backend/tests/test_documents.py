"""Document API tests — doc.ingest, doc.get, doc.list, doc.update, doc.link"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentLink,
    ExtractionField,
    Invoice,
    InvoiceLine,
    KnowledgeEdge,
    KnowledgeNode,
)


@pytest.mark.asyncio
async def test_ingest_document(client: AsyncClient):
    """doc.ingest — upload a file and get a Document back."""
    content = b"fake pdf content for testing"
    resp = await client.post(
        "/api/documents/ingest?source_channel=upload",
        files={"file": ("test.pdf", content, "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["file_name"] == "test.pdf"
    assert data["status"] == "ingested"
    assert data["is_duplicate"] is False
    assert "id" in data


@pytest.mark.asyncio
async def test_ingest_dedup(client: AsyncClient):
    """doc.ingest — duplicate file returns is_duplicate=True."""
    content = b"identical content for dedup test"
    resp1 = await client.post(
        "/api/documents/ingest",
        files={"file": ("a.pdf", content, "application/pdf")},
    )
    assert resp1.status_code == 200
    first_id = resp1.json()["id"]

    resp2 = await client.post(
        "/api/documents/ingest",
        files={"file": ("b.pdf", content, "application/pdf")},
    )
    assert resp2.status_code == 200
    data = resp2.json()
    assert data["is_duplicate"] is True
    assert data["duplicate_of"] == first_id


@pytest.mark.asyncio
async def test_list_documents(client: AsyncClient):
    """doc.list — returns paginated list."""
    # Ingest one document first
    await client.post(
        "/api/documents/ingest",
        files={"file": ("list_test.pdf", b"content", "application/pdf")},
    )

    resp = await client.get("/api/documents")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_get_document(client: AsyncClient):
    """doc.get — returns document with extractions and links."""
    ingest = await client.post(
        "/api/documents/ingest",
        files={"file": ("get_test.pdf", b"get content", "application/pdf")},
    )
    doc_id = ingest.json()["id"]

    resp = await client.get(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == doc_id
    assert "extractions" in data
    assert "links" in data


@pytest.mark.asyncio
async def test_get_document_management_summary(client: AsyncClient):
    """doc.management — returns pipeline, memory, graph and NTD counters."""
    ingest = await client.post(
        "/api/documents/ingest",
        files={"file": ("management_test.pdf", b"management content", "application/pdf")},
    )
    doc_id = ingest.json()["id"]

    resp = await client.get(f"/api/documents/{doc_id}/management")
    assert resp.status_code == 200
    data = resp.json()
    assert data["document"]["id"] == doc_id
    assert data["pipeline"]["extraction_count"] == 0
    assert data["pipeline"]["memory_chunks"] >= 0
    assert data["pipeline"]["graph_nodes"] >= 0
    assert data["links"] == []


@pytest.mark.asyncio
async def test_delete_document_removes_derived_records(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """doc.delete — hard-delete removes the document and derived DB records."""
    ingest = await client.post(
        "/api/documents/ingest",
        files={"file": ("delete_test.pdf", b"delete content", "application/pdf")},
    )
    doc_id = ingest.json()["id"]
    doc_uuid = uuid.UUID(doc_id)

    extraction = DocumentExtraction(document_id=doc_uuid, model_name="test")
    db_session.add(extraction)
    await db_session.flush()
    db_session.add(ExtractionField(extraction_id=extraction.id, field_name="number"))
    invoice = Invoice(document_id=doc_uuid)
    db_session.add(invoice)
    await db_session.flush()
    db_session.add(InvoiceLine(invoice_id=invoice.id, line_number=1))
    db_session.add(DocumentChunk(document_id=doc_uuid, chunk_index=1, text="chunk"))
    db_session.add(
        DocumentLink(
            document_id=doc_uuid,
            linked_entity_type="document",
            linked_entity_id=uuid.uuid4(),
            link_type="related",
        )
    )
    await db_session.commit()

    resp = await client.delete(f"/api/documents/{doc_id}?delete_files=false")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1
    assert await db_session.get(Document, doc_uuid) is None
    assert (await db_session.execute(select(DocumentExtraction))).scalars().all() == []
    assert (await db_session.execute(select(ExtractionField))).scalars().all() == []
    assert (await db_session.execute(select(Invoice))).scalars().all() == []
    assert (await db_session.execute(select(InvoiceLine))).scalars().all() == []
    remaining_chunks = await db_session.execute(
        select(DocumentChunk).where(DocumentChunk.document_id == doc_uuid)
    )
    remaining_links = await db_session.execute(
        select(DocumentLink).where(DocumentLink.document_id == doc_uuid)
    )
    assert remaining_chunks.scalars().all() == []
    assert remaining_links.scalars().all() == []


@pytest.mark.asyncio
async def test_get_document_not_found(client: AsyncClient):
    """doc.get — 404 for nonexistent document."""
    resp = await client.get("/api/documents/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_document(client: AsyncClient):
    """doc.update — patch document fields."""
    ingest = await client.post(
        "/api/documents/ingest",
        files={"file": ("update_test.pdf", b"update content", "application/pdf")},
    )
    doc_id = ingest.json()["id"]

    resp = await client.patch(
        f"/api/documents/{doc_id}",
        json={"doc_type": "invoice"},
    )
    assert resp.status_code == 200
    assert resp.json()["doc_type"] == "invoice"


@pytest.mark.asyncio
async def test_link_document(client: AsyncClient):
    """doc.link — link document to an entity."""
    ingest = await client.post(
        "/api/documents/ingest",
        files={"file": ("link_test.pdf", b"link content", "application/pdf")},
    )
    doc_id = ingest.json()["id"]

    resp = await client.post(
        f"/api/documents/{doc_id}/links",
        json={
            "linked_entity_type": "party",
            "linked_entity_id": "00000000-0000-0000-0000-000000000001",
            "link_type": "supplier",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["linked_entity_type"] == "party"


@pytest.mark.asyncio
async def test_document_dependencies_include_links_and_graph(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """doc.dependencies — returns explicit links and graph relations for document management."""
    ingest = await client.post(
        "/api/documents/ingest",
        files={"file": ("deps_test.pdf", b"deps content", "application/pdf")},
    )
    doc_id = ingest.json()["id"]
    linked_id = str(uuid.uuid4())
    await client.post(
        f"/api/documents/{doc_id}/links",
        json={
            "linked_entity_type": "document",
            "linked_entity_id": linked_id,
            "link_type": "supersedes",
        },
    )

    source = KnowledgeNode(
        node_type="document",
        title="Route card",
        source_document_id=uuid.UUID(doc_id),
    )
    target = KnowledgeNode(node_type="standard", title="GOST tolerance table")
    db_session.add_all([source, target])
    await db_session.flush()
    db_session.add(
        KnowledgeEdge(
            source_node_id=source.id,
            target_node_id=target.id,
            edge_type="requires",
            reason="Document references the tolerance table",
            source_document_id=uuid.UUID(doc_id),
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/documents/{doc_id}/dependencies?query=tolerance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_edges"] == 1
    assert data["links"][0]["link_type"] == "supersedes"


@pytest.mark.asyncio
async def test_delete_document_link(client: AsyncClient):
    """doc.link_delete — removes an explicit document link."""
    ingest = await client.post(
        "/api/documents/ingest",
        files={"file": ("unlink_test.pdf", b"unlink content", "application/pdf")},
    )
    doc_id = ingest.json()["id"]
    link = await client.post(
        f"/api/documents/{doc_id}/links",
        json={
            "linked_entity_type": "document",
            "linked_entity_id": str(uuid.uuid4()),
            "link_type": "related",
        },
    )

    resp = await client.delete(f"/api/documents/{doc_id}/links/{link.json()['id']}")
    assert resp.status_code == 204
    deps = await client.get(f"/api/documents/{doc_id}/dependencies")
    assert deps.json()["links"] == []


@pytest.mark.asyncio
async def test_bulk_delete_documents(client: AsyncClient):
    """doc.bulk_delete — hard-deletes selected documents."""
    ids = []
    for filename in ("bulk_a.pdf", "bulk_b.pdf"):
        ingest = await client.post(
            "/api/documents/ingest",
            files={"file": (filename, filename.encode(), "application/pdf")},
        )
        ids.append(ingest.json()["id"])

    resp = await client.request(
        "DELETE",
        "/api/documents/bulk-delete",
        json={"document_ids": ids, "delete_files": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] == 2

    for doc_id in ids:
        missing = await client.get(f"/api/documents/{doc_id}")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_development_purge_requires_confirmation(client: AsyncClient):
    """doc.dev_purge — full cleanup requires exact confirmation text."""
    resp = await client.post(
        "/api/documents/dev/purge-all",
        json={"confirm": "wrong", "delete_files": False},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_development_purge_all_documents(client: AsyncClient):
    """doc.dev_purge — deletes all document records for development cleanup."""
    await client.post(
        "/api/documents/ingest",
        files={"file": ("purge_a.pdf", b"a", "application/pdf")},
    )
    await client.post(
        "/api/documents/ingest",
        files={"file": ("purge_b.pdf", b"b", "application/pdf")},
    )

    resp = await client.post(
        "/api/documents/dev/purge-all",
        json={"confirm": "DELETE ALL DOCUMENT DATA", "delete_files": False},
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] >= 2
    listed = await client.get("/api/documents")
    assert listed.json()["total"] == 0
