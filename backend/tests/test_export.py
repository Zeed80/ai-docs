"""Tests for Export API — Excel and 1C export jobs."""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.db.models import Document, DocumentStatus, Invoice, InvoiceStatus


@pytest.fixture
async def invoice(db_session):
    doc = Document(
        file_name="export-test.pdf",
        file_hash="exp001",
        file_size=512,
        mime_type="application/pdf",
        storage_path="e/1.pdf",
        status=DocumentStatus.approved,
    )
    db_session.add(doc)
    await db_session.flush()

    inv = Invoice(
        document_id=doc.id,
        invoice_number="EXP-001",
        currency="RUB",
        total_amount=12000.0,
        status=InvoiceStatus.approved,
        invoice_date=datetime.now(timezone.utc),
    )
    db_session.add(inv)
    await db_session.commit()
    return inv


# ── Excel export ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_invoice_excel(client: AsyncClient, invoice):
    resp = await client.post(f"/api/invoices/{invoice.id}/export")
    assert resp.status_code == 202
    data = resp.json()
    assert data["entity_type"] == "invoice"
    assert data["entity_id"] == str(invoice.id)
    assert data["export_format"] == "excel"
    assert data["status"] == "pending"
    assert "id" in data


@pytest.mark.asyncio
async def test_export_invoice_excel_not_found(client: AsyncClient):
    resp = await client.post(f"/api/invoices/{uuid.uuid4()}/export")
    assert resp.status_code == 404


# ── 1C export ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_invoice_1c_creates_job(client: AsyncClient, invoice):
    resp = await client.post(f"/api/invoices/{invoice.id}/export-1c")
    assert resp.status_code == 202
    data = resp.json()
    assert data["export_format"] == "1c_xml"
    assert data["status"] == "pending"
    assert data["entity_id"] == str(invoice.id)


@pytest.mark.asyncio
async def test_export_invoice_1c_not_found(client: AsyncClient):
    resp = await client.post(f"/api/invoices/{uuid.uuid4()}/export-1c")
    assert resp.status_code == 404


# ── Export job list ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_export_jobs_empty(client: AsyncClient):
    resp = await client.get("/api/export-jobs")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_list_export_jobs_after_create(client: AsyncClient, invoice):
    await client.post(f"/api/invoices/{invoice.id}/export")
    resp = await client.get("/api/export-jobs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    formats = [j["export_format"] for j in data["items"]]
    assert "excel" in formats


@pytest.mark.asyncio
async def test_list_export_jobs_filter_by_status(client: AsyncClient, invoice):
    await client.post(f"/api/invoices/{invoice.id}/export")
    await client.post(f"/api/invoices/{invoice.id}/export-1c")

    resp = await client.get("/api/export-jobs", params={"status": "pending"})
    assert resp.status_code == 200
    for job in resp.json()["items"]:
        assert job["status"] == "pending"


# ── Get export job by ID ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_export_job(client: AsyncClient, invoice):
    create_resp = await client.post(f"/api/invoices/{invoice.id}/export")
    job_id = create_resp.json()["id"]

    resp = await client.get(f"/api/export-jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == job_id
    assert data["export_format"] == "excel"


@pytest.mark.asyncio
async def test_get_export_job_not_found(client: AsyncClient):
    resp = await client.get(f"/api/export-jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Download endpoint ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_export_not_ready(client: AsyncClient, invoice):
    create_resp = await client.post(f"/api/invoices/{invoice.id}/export")
    job_id = create_resp.json()["id"]

    resp = await client.get(f"/api/export-jobs/{job_id}/download", follow_redirects=False)
    # Job is still pending, should return 400
    assert resp.status_code == 400
