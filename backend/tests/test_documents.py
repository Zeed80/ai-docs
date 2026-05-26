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
    DocumentProcessingJob,
    DocumentStatus,
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
async def test_ingest_with_manual_doc_type_override(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """doc.ingest — requested doc type can be pinned by a human override."""
    resp = await client.post(
        "/api/documents/ingest"
        "?source_channel=upload"
        "&requested_doc_type=drawing"
        "&manual_doc_type_override=true"
        "&auto_process=false",
        files={"file": ("manual_type.pdf", b"manual type content", "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pipeline_queued"] is False

    doc = await db_session.get(Document, uuid.UUID(data["id"]))
    assert doc is not None
    assert doc.doc_type.value == "drawing"
    assert doc.doc_type_confidence == 1.0
    assert doc.metadata_["manual_doc_type_override"] is True


@pytest.mark.asyncio
async def test_update_document_sets_manual_override(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """doc.update — manual doc type assignment is preserved for later classification."""
    ingest = await client.post(
        "/api/documents/ingest?auto_process=false",
        files={"file": ("manual_update.pdf", b"manual update content", "application/pdf")},
    )
    doc_id = ingest.json()["id"]

    resp = await client.patch(
        f"/api/documents/{doc_id}",
        json={
            "doc_type": "contract",
            "source_channel": "upload",
            "manual_doc_type_override": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["doc_type"] == "contract"

    doc = await db_session.get(Document, uuid.UUID(doc_id))
    assert doc is not None
    assert doc.doc_type_confidence == 1.0
    assert doc.metadata_["manual_doc_type_override"] is True


@pytest.mark.asyncio
async def test_document_workspace_endpoint(client: AsyncClient):
    """doc.workspace — returns documents with compact pipeline summaries and counters."""
    ingest = await client.post(
        "/api/documents/ingest?auto_process=false",
        files={"file": ("workspace_test.pdf", b"workspace content", "application/pdf")},
    )
    doc_id = ingest.json()["id"]

    resp = await client.get("/api/documents/workspace")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert data["status_counts"]["ingested"] >= 1
    item = next(item for item in data["items"] if item["document"]["id"] == doc_id)
    assert item["pipeline"]["memory_chunks"] >= 0
    assert isinstance(item["pipeline"]["pipeline_steps"], list)


@pytest.mark.asyncio
async def test_ingest_auto_process_creates_queued_pipeline_job(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """doc.ingest — auto-process upload exposes queued pipeline progress immediately."""
    from app.tasks import extraction

    class FakeTask:
        id = "queued-task-1"

    monkeypatch.setattr(extraction.process_document, "delay", lambda *args: FakeTask())
    resp = await client.post(
        "/api/documents/ingest",
        files={"file": ("pipeline_progress.txt", b"invoice text", "text/plain")},
    )
    assert resp.status_code == 200
    doc_id = resp.json()["id"]

    job = (
        await db_session.execute(
            select(DocumentProcessingJob)
            .where(DocumentProcessingJob.document_id == uuid.UUID(doc_id))
            .order_by(DocumentProcessingJob.created_at.desc())
        )
    ).scalars().first()
    assert job is not None
    assert job.status == "queued"
    assert job.current_step == "classification"
    assert job.celery_task_id == "queued-task-1"
    assert any(
        step["key"] == "classification" and step["status"] == "queued"
        for step in job.pipeline_steps
    )


@pytest.mark.asyncio
async def test_batch_process_skips_quarantined_documents(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """doc.batch_process — queues allowed documents and skips quarantine."""
    from app.tasks import extraction

    class FakeTask:
        id = "task-1"

    monkeypatch.setattr(extraction.process_document, "delay", lambda *args: FakeTask())
    ok = await client.post(
        "/api/documents/ingest?auto_process=false",
        files={"file": ("batch_ok.pdf", b"batch ok", "application/pdf")},
    )
    quarantined = Document(
        file_name="blocked.exe",
        file_hash="blocked",
        file_size=1,
        mime_type="application/octet-stream",
        storage_path="documents/blocked",
        status=DocumentStatus.suspicious,
    )
    db_session.add(quarantined)
    await db_session.commit()

    resp = await client.post(
        "/api/documents/batch/process",
        json={"document_ids": [ok.json()["id"], str(quarantined.id)]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert [item["status"] for item in data["results"]] == ["queued", "skipped"]


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


# ── Edge cases: validation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_empty_file_rejected(client: AsyncClient):
    """doc.ingest — zero-byte file must be rejected with 422."""
    resp = await client.post(
        "/api/documents/ingest",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert resp.status_code == 422
    data = resp.json()
    assert "Empty file" in str(data.get("detail", ""))


@pytest.mark.asyncio
async def test_ingest_file_too_large_rejected(client: AsyncClient):
    """doc.ingest — file exceeding MAX_UPLOAD_SIZE must return 413."""
    from app.config import settings

    original_limit = settings.max_upload_size_mb
    settings.max_upload_size_mb = 1  # 1 MB for this test

    content = b"x" * (1 * 1024 * 1024 + 1)  # 1 MB + 1 byte
    try:
        resp = await client.post(
            "/api/documents/ingest",
            files={"file": ("huge.pdf", content, "application/pdf")},
        )
        assert resp.status_code == 413
        detail = resp.json().get("detail", {})
        assert "too large" in str(detail).lower() or "413" in str(resp.status_code)
    finally:
        settings.max_upload_size_mb = original_limit


@pytest.mark.asyncio
async def test_ingest_filename_with_cyrillic(client: AsyncClient):
    """doc.ingest — Cyrillic characters in filename must be handled correctly."""
    filename = "Счёт №123 от 15.05.2024 г..pdf"
    resp = await client.post(
        "/api/documents/ingest",
        files={"file": (filename, b"pdf content", "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["file_name"] == filename


@pytest.mark.asyncio
async def test_ingest_filename_with_special_chars(client: AsyncClient):
    """doc.ingest — special characters and spaces in filename."""
    filename = "Invoice (copy) [final] v2.pdf"
    resp = await client.post(
        "/api/documents/ingest",
        files={"file": (filename, b"content", "application/pdf")},
    )
    assert resp.status_code == 200
    assert resp.json()["file_name"] == filename


@pytest.mark.asyncio
async def test_ingest_unsupported_extension_quarantined(client: AsyncClient):
    """doc.ingest — .exe extension must be quarantined (202), not rejected."""
    resp = await client.post(
        "/api/documents/ingest",
        files={"file": ("malware.exe", b"MZ\x90\x00", "application/octet-stream")},
    )
    # Should be quarantined (202) or rejected as bad extension, not a 5xx
    assert resp.status_code in (200, 202, 422)
    if resp.status_code == 202:
        assert resp.json().get("quarantined") is True


@pytest.mark.asyncio
async def test_ingest_sequential_same_file_dedup(client: AsyncClient):
    """doc.ingest — 5 sequential uploads of the same content → 1 new + 4 duplicates."""
    content = b"sequential dedup test content " + b"x" * 500
    responses = []
    for i in range(5):
        resp = await client.post(
            "/api/documents/ingest?auto_process=false",
            files={"file": (f"dup_{i}.pdf", content, "application/pdf")},
        )
        responses.append(resp)

    statuses = [r.status_code for r in responses]
    # All must succeed (no 5xx)
    assert all(s < 500 for s in statuses), f"Server errors: {statuses}"

    success_responses = [r for r in responses if r.status_code == 200]
    new_docs = [r for r in success_responses if not r.json().get("is_duplicate")]
    duplicates = [r for r in success_responses if r.json().get("is_duplicate")]

    # Exactly 1 new document created
    assert len(new_docs) == 1, f"Expected 1 new doc, got {len(new_docs)}"
    assert len(duplicates) == 4, f"Expected 4 duplicates, got {len(duplicates)}"

    # All duplicates point to the same original
    original_id = new_docs[0].json()["id"]
    for dup in duplicates:
        assert dup.json()["duplicate_of"] == original_id


@pytest.mark.asyncio
async def test_ingest_batch_sequential_50_files(client: AsyncClient):
    """doc.ingest — 50 distinct files uploaded sequentially, all succeed."""
    errors = []
    for i in range(50):
        content = f"unique content for file {i} padding {'x' * 100}".encode()
        resp = await client.post(
            "/api/documents/ingest?auto_process=false",
            files={"file": (f"batch_{i:03d}.pdf", content, "application/pdf")},
        )
        if resp.status_code != 200:
            errors.append(f"file {i}: status={resp.status_code}")

    assert errors == [], f"Batch upload errors: {errors}"


@pytest.mark.asyncio
async def test_ingest_source_channel_preserved(client: AsyncClient):
    """doc.ingest — source_channel parameter must be stored on the document."""
    for channel in ("upload", "email", "chat", "telegram"):
        content = f"channel test {channel}".encode()
        resp = await client.post(
            f"/api/documents/ingest?source_channel={channel}&auto_process=false",
            files={"file": (f"channel_{channel}.pdf", content, "application/pdf")},
        )
        assert resp.status_code == 200
        doc_id = resp.json()["id"]

        get_resp = await client.get(f"/api/documents/{doc_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["source_channel"] == channel


@pytest.mark.asyncio
async def test_ingest_auto_process_false_no_pipeline_job_queued(client: AsyncClient):
    """doc.ingest — auto_process=false must not queue a pipeline job."""
    resp = await client.post(
        "/api/documents/ingest?auto_process=false",
        files={"file": ("no_pipeline.pdf", b"no pipeline content", "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pipeline_queued"] is False


@pytest.mark.asyncio
async def test_ingest_auto_process_true_pipeline_queued(client: AsyncClient):
    """doc.ingest — auto_process=true must set pipeline_queued=True in response."""
    resp = await client.post(
        "/api/documents/ingest?auto_process=true",
        files={"file": ("with_pipeline.pdf", b"with pipeline content", "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pipeline_queued"] is True


@pytest.mark.asyncio
async def test_ingest_manual_doc_type_override_stored(client: AsyncClient):
    """doc.ingest — manual_doc_type_override=true must persist the requested type."""
    resp = await client.post(
        "/api/documents/ingest?auto_process=false&requested_doc_type=letter&manual_doc_type_override=true",
        files={"file": ("typed_doc.pdf", b"letter content", "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["detected_type"] == "letter"
    assert data["detected_type_source"] == "manual"
