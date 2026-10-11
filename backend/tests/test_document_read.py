"""documents.read — the agent reads what a document says (E50).

Asked for an e-mail written in a contract, the agent could list, search,
extract (re-processing) and summarize (lossy) but not read the text.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.auth.models import UserInfo, UserRole
from app.db.models import Document, DocumentChunk, DocumentStatus, DocumentType

TEXT = "ДОГОВОР ПОСТАВКИ № ДП-77\n\nE-mail для счетов: buh@eval-stal.example\n\n" + "Пункт. " * 400


def _user(sub: str) -> UserInfo:
    return UserInfo(
        sub=sub, email=f"{sub}@x", name=sub, preferred_username=sub, roles=[UserRole.buyer]
    )


@pytest.fixture
def as_user(client: AsyncClient):
    from app.auth.acting import get_effective_user
    from app.auth.jwt import get_current_user
    from app.main import app

    def bind(user: UserInfo):
        app.dependency_overrides[get_effective_user] = lambda: user
        app.dependency_overrides[get_current_user] = lambda: user

    yield bind
    app.dependency_overrides.pop(get_effective_user, None)
    app.dependency_overrides.pop(get_current_user, None)


async def _pdf_document(db, owner: str) -> Document:
    from app.ai.evals.live_runner import _pdf_bytes
    from app.storage import upload_file

    content = _pdf_bytes(TEXT)
    path = f"tests/read/{uuid.uuid4()}.pdf"
    upload_file(content, path, "application/pdf")
    doc = Document(
        owner_sub=owner,
        file_name="ДП-77.pdf",
        file_hash=uuid.uuid4().hex,
        file_size=len(content),
        mime_type="application/pdf",
        storage_path=path,
        doc_type=DocumentType.contract,
        status=DocumentStatus.approved,
    )
    db.add(doc)
    await db.commit()
    return doc


@pytest.mark.asyncio
async def test_read_returns_the_text_in_pages(client: AsyncClient, db_session, as_user):
    as_user(_user("alice"))
    doc = await _pdf_document(db_session, "alice")
    first = await client.get(f"/api/documents/{doc.id}/text", params={"limit": 1000})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["source"] == "pdf_text_layer"
    assert "buh@eval-stal.example" in body["text"]
    assert body["next_offset"] == 1000 and body["total_chars"] > 1000
    rest = await client.get(
        f"/api/documents/{doc.id}/text", params={"offset": body["next_offset"], "limit": 20000}
    )
    assert rest.json()["next_offset"] is None
    assert len(body["text"]) + len(rest.json()["text"]) == body["total_chars"]


@pytest.mark.asyncio
async def test_read_falls_back_to_ocr_chunks(client: AsyncClient, db_session, as_user):
    as_user(_user("alice"))
    doc = Document(
        owner_sub="alice",
        file_name="скан.jpg",
        file_hash=uuid.uuid4().hex,
        file_size=1,
        mime_type="image/jpeg",
        storage_path="tests/read/none.jpg",
        status=DocumentStatus.approved,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add_all(
        [
            DocumentChunk(document_id=doc.id, chunk_index=1, text="второй абзац"),
            DocumentChunk(document_id=doc.id, chunk_index=0, text="первый абзац"),
        ]
    )
    await db_session.commit()
    resp = await client.get(f"/api/documents/{doc.id}/text")
    assert resp.json()["source"] == "ocr_chunks"
    assert resp.json()["text"] == "первый абзац\n\nвторой абзац"


@pytest.mark.asyncio
async def test_someone_elses_document_reads_as_missing(client: AsyncClient, db_session, as_user):
    doc = await _pdf_document(db_session, "alice")
    as_user(_user("bob"))
    resp = await client.get(f"/api/documents/{doc.id}/text")
    assert resp.status_code == 404


def test_read_is_a_reviewed_read_action_of_documents():
    from app.ai.tool_catalog import get_tool

    tool = get_tool("documents", "read")
    assert tool is not None and tool.effect == "read"
    assert tool.path == "/api/documents/{document_id}/text"


@pytest.mark.asyncio
async def test_get_carries_the_beginning_of_the_text(client: AsyncClient, db_session, as_user):
    """Asked about a contract's content, the agent re-ran extraction after
    a get that held no text (E50 live run)."""
    as_user(_user("alice"))
    doc = await _pdf_document(db_session, "alice")
    db_session.add(DocumentChunk(document_id=doc.id, chunk_index=0, text=TEXT))
    await db_session.commit()
    body = (await client.get(f"/api/documents/{doc.id}")).json()
    assert "buh@eval-stal.example" in body["text_preview"]
    assert "documents.read" in body["text_hint"]


@pytest.mark.asyncio
async def test_the_agent_does_not_reprocess_a_processed_document(
    client: AsyncClient, db_session, as_user
):
    agent = _user("alice").model_copy(update={"via_agent": True})
    as_user(agent)
    doc = await _pdf_document(db_session, "alice")
    db_session.add(DocumentChunk(document_id=doc.id, chunk_index=0, text=TEXT))
    await db_session.commit()
    resp = await client.post(f"/api/documents/{doc.id}/extract")
    assert resp.status_code == 409
    assert "documents.read" in resp.json()["detail"]["message"]
