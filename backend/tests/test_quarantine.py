"""Tests for Quarantine API — review/release quarantined files, allowlist."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import Document, DocumentStatus, QuarantineEntry, FileExtensionAllowlist


@pytest.fixture
async def quarantine_doc(db_session):
    doc = Document(
        file_name="suspicious.exe",
        file_hash="quar001",
        file_size=1024,
        mime_type="application/octet-stream",
        storage_path="q/suspicious.exe",
        status=DocumentStatus.suspicious,
    )
    db_session.add(doc)
    await db_session.flush()

    entry = QuarantineEntry(
        document_id=doc.id,
        reason="extension_not_allowed",
        original_filename="suspicious.exe",
        detected_mime="application/octet-stream",
    )
    db_session.add(entry)
    await db_session.commit()
    return entry


@pytest.fixture
async def pdf_doc(db_session):
    doc = Document(
        file_name="valid.pdf",
        file_hash="quar002",
        file_size=512,
        mime_type="application/pdf",
        storage_path="q/valid.pdf",
        status=DocumentStatus.suspicious,
    )
    db_session.add(doc)
    await db_session.flush()

    entry = QuarantineEntry(
        document_id=doc.id,
        reason="mime_mismatch",
        original_filename="valid.pdf",
        detected_mime="application/pdf",
    )
    db_session.add(entry)
    await db_session.commit()
    return entry


# ── List ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_quarantine_empty(client: AsyncClient):
    resp = await client.get("/api/quarantine")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_list_quarantine_includes_entry(client: AsyncClient, quarantine_doc):
    resp = await client.get("/api/quarantine", params={"pending_only": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    ids = [i["id"] for i in data["items"]]
    assert str(quarantine_doc.id) in ids


@pytest.mark.asyncio
async def test_quarantine_count(client: AsyncClient, quarantine_doc):
    resp = await client.get("/api/quarantine/count")
    assert resp.status_code == 200
    data = resp.json()
    assert "count" in data
    assert data["count"] >= 1


# ── Release ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_quarantine(client: AsyncClient, pdf_doc):
    resp = await client.post(f"/api/quarantine/{pdf_doc.id}/release")
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "released"
    assert data["reviewed_by"] == "user"


@pytest.mark.asyncio
async def test_release_already_decided(client: AsyncClient, pdf_doc):
    await client.post(f"/api/quarantine/{pdf_doc.id}/release")
    # Try releasing again
    resp = await client.post(f"/api/quarantine/{pdf_doc.id}/release")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_release_not_found(client: AsyncClient):
    resp = await client.post(f"/api/quarantine/{uuid.uuid4()}/release")
    assert resp.status_code == 404


# ── Delete ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_quarantine_entry(client: AsyncClient, quarantine_doc):
    resp = await client.delete(f"/api/quarantine/{quarantine_doc.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "deleted"


@pytest.mark.asyncio
async def test_delete_quarantine_not_found(client: AsyncClient):
    resp = await client.delete(f"/api/quarantine/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Allowlist ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_allowlist(client: AsyncClient):
    resp = await client.get("/api/quarantine/allowlist")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_add_to_allowlist(client: AsyncClient):
    resp = await client.post("/api/quarantine/allowlist", json={
        "extension": ".xyz_test",
        "is_allowed": True,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["extension"] == ".xyz_test"
    assert data["is_allowed"] is True


@pytest.mark.asyncio
async def test_remove_from_allowlist(client: AsyncClient):
    create_resp = await client.post("/api/quarantine/allowlist", json={
        "extension": ".tmp_del_test",
        "is_allowed": True,
    })
    entry_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/quarantine/allowlist/{entry_id}")
    assert resp.status_code == 204
