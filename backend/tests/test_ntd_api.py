"""Tests for NTD API — normative documents, settings, requirements, checks."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import NormativeDocument, Document, DocumentStatus


@pytest.fixture
async def normative_doc(db_session):
    nd = NormativeDocument(
        code="ГОСТ 14.004-83",
        title="Технологическая подготовка производства",
        document_type="ГОСТ",
        status="active",
    )
    db_session.add(nd)
    await db_session.commit()
    return nd


@pytest.fixture
async def source_doc(db_session):
    doc = Document(
        file_name="gost-test.pdf",
        file_hash="ntd001",
        file_size=1024,
        mime_type="application/pdf",
        storage_path="n/1.pdf",
        status=DocumentStatus.approved,
    )
    db_session.add(doc)
    await db_session.commit()
    return doc


# ── NTD Control Settings ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_ntd_control_settings(client: AsyncClient):
    resp = await client.get("/api/settings/ntd-control")
    assert resp.status_code == 200
    data = resp.json()
    assert "mode" in data


@pytest.mark.asyncio
async def test_update_ntd_control_settings(client: AsyncClient):
    resp = await client.patch("/api/settings/ntd-control", json={
        "mode": "auto",
        "updated_by": "engineer",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "auto"


# ── Normative Documents ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_normative_documents_empty(client: AsyncClient):
    resp = await client.get("/api/ntd/documents")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_list_normative_documents(client: AsyncClient, normative_doc):
    resp = await client.get("/api/ntd/documents")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    codes = [d["code"] for d in data]
    assert "ГОСТ 14.004-83" in codes


@pytest.mark.asyncio
async def test_create_normative_document(client: AsyncClient):
    resp = await client.post("/api/ntd/documents", json={
        "code": "ГОСТ 2.104-2006",
        "title": "Основная надпись",
        "document_type": "ГОСТ",
        "status": "active",
        "version": "1.0",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["code"] == "ГОСТ 2.104-2006"
    assert data["document_type"] == "ГОСТ"
    assert "id" in data


@pytest.mark.asyncio
async def test_create_normative_clause(client: AsyncClient, normative_doc):
    resp = await client.post("/api/ntd/clauses", json={
        "normative_document_id": str(normative_doc.id),
        "clause_number": "1.1",
        "title": "Общие положения",
        "text": "Настоящий стандарт распространяется на...",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["clause_number"] == "1.1"
    assert data["normative_document_id"] == str(normative_doc.id)


@pytest.mark.asyncio
async def test_create_normative_requirement(client: AsyncClient, normative_doc):
    resp = await client.post("/api/ntd/requirements", json={
        "normative_document_id": str(normative_doc.id),
        "requirement_code": "P-4.2",
        "requirement_type": "surface",
        "text": "Чистота поверхности Ra ≤ 1.6",
        "severity": "warning",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["text"] == "Чистота поверхности Ra ≤ 1.6"
    assert data["normative_document_id"] == str(normative_doc.id)


# ── Requirements search ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_requirements_empty(client: AsyncClient):
    resp = await client.get("/api/ntd/requirements/search", params={"query": "нет такого требования xyz123"})
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data or "hits" in data or isinstance(data, dict)


# ── NTD Check Availability ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ntd_check_availability(client: AsyncClient, source_doc):
    resp = await client.get(f"/api/documents/{source_doc.id}/ntd-check/availability")
    assert resp.status_code == 200
    data = resp.json()
    assert "can_check" in data
    assert "mode" in data


@pytest.mark.asyncio
async def test_list_ntd_checks_for_document(client: AsyncClient, source_doc):
    resp = await client.get(f"/api/documents/{source_doc.id}/ntd-checks")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
