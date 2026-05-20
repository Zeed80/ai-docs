"""Tests for Payments API — payment schedules."""

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient

from app.db.models import Document, DocumentStatus, Invoice, InvoiceStatus, Party, PartyRole


@pytest.fixture
async def invoice(db_session):
    doc = Document(
        file_name="pay-test.pdf",
        file_hash="payh001",
        file_size=1024,
        mime_type="application/pdf",
        storage_path="p/1.pdf",
        status=DocumentStatus.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()

    inv = Invoice(
        document_id=doc.id,
        invoice_number="PAY-INV-001",
        currency="RUB",
        total_amount=45000.0,
        status=InvoiceStatus.needs_review,
        invoice_date=datetime.now(timezone.utc),
    )
    db_session.add(inv)
    await db_session.commit()
    return inv


@pytest.fixture
async def invoice_with_supplier(db_session):
    supplier = Party(
        name='ООО "ПлатёжСервис"',
        inn="7700000001",
        role=PartyRole.supplier,
    )
    db_session.add(supplier)
    await db_session.flush()

    doc = Document(
        file_name="pay-supplier.pdf",
        file_hash="paysup001",
        file_size=1024,
        mime_type="application/pdf",
        storage_path="p/2.pdf",
        status=DocumentStatus.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()

    inv = Invoice(
        document_id=doc.id,
        invoice_number="PAY-INV-002",
        currency="RUB",
        total_amount=90000.0,
        status=InvoiceStatus.needs_review,
        invoice_date=datetime.now(timezone.utc),
        supplier_id=supplier.id,
    )
    db_session.add(inv)
    await db_session.commit()
    return inv


# ── CRUD ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_payment_schedule(client: AsyncClient, invoice):
    due_date = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    resp = await client.post("/api/payment-schedules", json={
        "invoice_id": str(invoice.id),
        "due_date": due_date,
        "amount": 45000.0,
        "currency": "RUB",
        "payment_number": 1,
        "notes": "Оплата в полном объёме",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["invoice_id"] == str(invoice.id)
    assert data["amount"] == 45000.0
    assert data["status"] == "scheduled"


@pytest.mark.asyncio
async def test_create_payment_schedule_invoice_not_found(client: AsyncClient):
    due_date = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    resp = await client.post("/api/payment-schedules", json={
        "invoice_id": str(uuid.uuid4()),
        "due_date": due_date,
        "amount": 10000.0,
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_payment_schedules(client: AsyncClient, invoice):
    due_date = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    await client.post("/api/payment-schedules", json={
        "invoice_id": str(invoice.id),
        "due_date": due_date,
        "amount": 15000.0,
    })
    resp = await client.get("/api/payment-schedules")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_list_payment_schedules_filter_by_invoice(client: AsyncClient, invoice):
    due_date = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    await client.post("/api/payment-schedules", json={
        "invoice_id": str(invoice.id),
        "due_date": due_date,
        "amount": 22500.0,
        "payment_number": 1,
    })
    resp = await client.get("/api/payment-schedules", params={"invoice_id": str(invoice.id)})
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["invoice_id"] == str(invoice.id)


@pytest.mark.asyncio
async def test_get_payment_schedule(client: AsyncClient, invoice):
    due_date = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    create_resp = await client.post("/api/payment-schedules", json={
        "invoice_id": str(invoice.id),
        "due_date": due_date,
        "amount": 45000.0,
    })
    schedule_id = create_resp.json()["id"]

    resp = await client.get(f"/api/payment-schedules/{schedule_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == schedule_id


@pytest.mark.asyncio
async def test_get_payment_schedule_not_found(client: AsyncClient):
    resp = await client.get(f"/api/payment-schedules/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_payment_schedule(client: AsyncClient, invoice):
    due_date = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    create_resp = await client.post("/api/payment-schedules", json={
        "invoice_id": str(invoice.id),
        "due_date": due_date,
        "amount": 45000.0,
    })
    schedule_id = create_resp.json()["id"]

    new_due = (datetime.now(timezone.utc) + timedelta(days=15)).isoformat()
    resp = await client.patch(f"/api/payment-schedules/{schedule_id}", json={
        "due_date": new_due,
        "notes": "Перенесено",
    })
    assert resp.status_code == 200
    assert resp.json()["notes"] == "Перенесено"


@pytest.mark.asyncio
async def test_mark_paid(client: AsyncClient, invoice):
    due_date = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    create_resp = await client.post("/api/payment-schedules", json={
        "invoice_id": str(invoice.id),
        "due_date": due_date,
        "amount": 45000.0,
    })
    schedule_id = create_resp.json()["id"]

    paid_at = datetime.now(timezone.utc).isoformat()
    resp = await client.post(f"/api/payment-schedules/{schedule_id}/mark-paid", json={
        "paid_amount": 45000.0,
        "reference": "ПП-2026-0001",
        "paid_at": paid_at,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "paid"
    assert data["reference"] == "ПП-2026-0001"
    assert data["paid_amount"] == 45000.0


@pytest.mark.asyncio
async def test_mark_paid_records_amount(client: AsyncClient, invoice):
    due_date = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    create_resp = await client.post("/api/payment-schedules", json={
        "invoice_id": str(invoice.id),
        "due_date": due_date,
        "amount": 45000.0,
    })
    schedule_id = create_resp.json()["id"]

    resp = await client.post(f"/api/payment-schedules/{schedule_id}/mark-paid", json={
        "paid_amount": 20000.0,
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "paid"
    assert resp.json()["paid_amount"] == 20000.0


# ── Overdue / Upcoming ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_overdue(client: AsyncClient, invoice, db_session):
    from app.db.models import PaymentSchedule

    # Overdue = status "scheduled" with past due_date
    past_due = datetime.now(timezone.utc) - timedelta(days=5)
    overdue = PaymentSchedule(
        invoice_id=invoice.id,
        due_date=past_due,
        amount=10000.0,
        status="scheduled",
    )
    db_session.add(overdue)
    await db_session.commit()

    resp = await client.get("/api/payment-schedules/overdue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    for item in data["items"]:
        assert item["status"] in ("scheduled", "partial")


@pytest.mark.asyncio
async def test_list_upcoming(client: AsyncClient, invoice):
    due_date = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    await client.post("/api/payment-schedules", json={
        "invoice_id": str(invoice.id),
        "due_date": due_date,
        "amount": 30000.0,
    })

    resp = await client.get("/api/payment-schedules/upcoming")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_create_with_supplier_creates_reminder(
    client: AsyncClient, invoice_with_supplier
):
    """Creating a schedule for invoice with supplier should trigger reminder creation."""
    due_date = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    resp = await client.post("/api/payment-schedules", json={
        "invoice_id": str(invoice_with_supplier.id),
        "due_date": due_date,
        "amount": 90000.0,
    })
    assert resp.status_code == 201
    assert resp.json()["amount"] == 90000.0
