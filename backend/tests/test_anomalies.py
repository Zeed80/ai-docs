"""Tests for Anomaly Detection API."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import (
    AnomalyCard,
    AnomalySeverity,
    AnomalyStatus,
    AnomalyType,
    Document,
    DocumentStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Party,
    PartyRole,
)


@pytest.fixture
async def supplier_with_invoices(db_session):
    """Supplier with 2 invoices — same number for duplicate detection."""
    party = Party(
        name="ООО Аномалия",
        inn="1234567890",
        role=PartyRole.supplier,
    )
    db_session.add(party)
    await db_session.flush()

    doc1 = Document(
        file_name="a1.pdf", file_hash="ah1", file_size=100,
        mime_type="application/pdf", storage_path="a/1.pdf",
        status=DocumentStatus.approved,
    )
    doc2 = Document(
        file_name="a2.pdf", file_hash="ah2", file_size=200,
        mime_type="application/pdf", storage_path="a/2.pdf",
        status=DocumentStatus.approved,
    )
    db_session.add_all([doc1, doc2])
    await db_session.flush()

    inv1 = Invoice(
        document_id=doc1.id, invoice_number="DUP-001", currency="RUB",
        total_amount=10000.0, status=InvoiceStatus.approved,
        supplier_id=party.id,
        metadata_={"supplier_bank_account": "40702810000000000001", "supplier_bik": "044525225"},
    )
    inv2 = Invoice(
        document_id=doc2.id, invoice_number="DUP-001", currency="RUB",
        total_amount=12000.0, status=InvoiceStatus.needs_review,
        supplier_id=party.id,
        metadata_={"supplier_bank_account": "40702810000000000999", "supplier_bik": "044525225"},
    )
    db_session.add_all([inv1, inv2])
    await db_session.flush()

    # Lines for price spike detection: inv1 has Bolt at 50, inv2 at 80 (+60%)
    line1 = InvoiceLine(
        invoice_id=inv1.id, line_number=1, description="Болт М10",
        quantity=100, unit="шт", unit_price=50.0, amount=5000.0,
    )
    line2 = InvoiceLine(
        invoice_id=inv2.id, line_number=1, description="Болт М10",
        quantity=100, unit="шт", unit_price=80.0, amount=8000.0,
    )
    db_session.add_all([line1, line2])
    await db_session.commit()
    return {"party": party, "inv1": inv1, "inv2": inv2}


@pytest.fixture
async def new_supplier_invoice(db_session):
    """A supplier with only ONE invoice — triggers new_supplier detector."""
    party = Party(
        name="ООО Новичок",
        inn="9876543210",
        role=PartyRole.supplier,
    )
    db_session.add(party)
    await db_session.flush()

    doc = Document(
        file_name="new.pdf", file_hash="newh", file_size=100,
        mime_type="application/pdf", storage_path="n/1.pdf",
        status=DocumentStatus.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()

    inv = Invoice(
        document_id=doc.id, invoice_number="NEW-001", currency="RUB",
        total_amount=5000.0, status=InvoiceStatus.needs_review,
        supplier_id=party.id,
    )
    db_session.add(inv)
    await db_session.flush()

    line = InvoiceLine(
        invoice_id=inv.id, line_number=1, description="Неизвестная деталь XYZ",
        quantity=10, unit="шт", unit_price=500.0, amount=5000.0,
    )
    db_session.add(line)
    await db_session.commit()
    return {"party": party, "inv": inv}


@pytest.mark.asyncio
async def test_check_duplicate_and_price_spike(client: AsyncClient, supplier_with_invoices):
    """Check detects duplicate invoice and price spike."""
    inv2_id = str(supplier_with_invoices["inv2"].id)
    resp = await client.post("/api/anomalies/check", json={"invoice_id": inv2_id})
    assert resp.status_code == 200
    data = resp.json()
    assert data["anomalies_found"] >= 2

    types = [a["anomaly_type"] for a in data["anomalies"]]
    assert "duplicate" in types
    assert "price_spike" in types


@pytest.mark.asyncio
async def test_check_requisite_change(client: AsyncClient, supplier_with_invoices):
    """Inv2 has different bank_account from inv1 — should detect requisite_change."""
    inv2_id = str(supplier_with_invoices["inv2"].id)
    resp = await client.post("/api/anomalies/check", json={"invoice_id": inv2_id})
    assert resp.status_code == 200
    types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
    assert "requisite_change" in types


@pytest.mark.asyncio
async def test_check_new_supplier(client: AsyncClient, new_supplier_invoice):
    """First invoice from supplier — should detect new_supplier."""
    inv_id = str(new_supplier_invoice["inv"].id)
    resp = await client.post("/api/anomalies/check", json={"invoice_id": inv_id})
    assert resp.status_code == 200
    types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
    assert "new_supplier" in types


@pytest.mark.asyncio
async def test_check_unknown_items(client: AsyncClient, new_supplier_invoice):
    """All lines without canonical_item_id — should detect unknown_item."""
    inv_id = str(new_supplier_invoice["inv"].id)
    resp = await client.post("/api/anomalies/check", json={"invoice_id": inv_id})
    assert resp.status_code == 200
    types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
    assert "unknown_item" in types


@pytest.mark.asyncio
async def test_create_anomaly(client: AsyncClient, supplier_with_invoices):
    inv_id = str(supplier_with_invoices["inv1"].id)
    resp = await client.post("/api/anomalies", json={
        "anomaly_type": "duplicate",
        "severity": "warning",
        "entity_type": "invoice",
        "entity_id": inv_id,
        "title": "Тестовая аномалия",
        "description": "Ручное создание",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["anomaly_type"] == "duplicate"
    assert data["status"] == "open"
    assert data["title"] == "Тестовая аномалия"
    return data["id"]


@pytest.mark.asyncio
async def test_list_anomalies(client: AsyncClient, supplier_with_invoices):
    """Create one, then list."""
    inv_id = str(supplier_with_invoices["inv1"].id)
    await client.post("/api/anomalies", json={
        "anomaly_type": "price_spike",
        "severity": "warning",
        "entity_type": "invoice",
        "entity_id": inv_id,
        "title": "Скачок цены",
    })

    resp = await client.get("/api/anomalies")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_list_filter_by_severity(client: AsyncClient, supplier_with_invoices):
    inv_id = str(supplier_with_invoices["inv1"].id)
    await client.post("/api/anomalies", json={
        "anomaly_type": "duplicate",
        "severity": "critical",
        "entity_type": "invoice",
        "entity_id": inv_id,
        "title": "Критичная",
    })
    resp = await client.get("/api/anomalies", params={"severity": "critical"})
    assert resp.status_code == 200
    for a in resp.json():
        assert a["severity"] == "critical"


@pytest.mark.asyncio
async def test_resolve_anomaly(client: AsyncClient, supplier_with_invoices):
    inv_id = str(supplier_with_invoices["inv1"].id)
    create_resp = await client.post("/api/anomalies", json={
        "anomaly_type": "new_supplier",
        "severity": "info",
        "entity_type": "invoice",
        "entity_id": inv_id,
        "title": "Для резолюции",
    })
    anomaly_id = create_resp.json()["id"]

    resp = await client.post(f"/api/anomalies/{anomaly_id}/resolve", json={
        "resolution": "false_positive",
        "comment": "Проверено, всё ОК",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "false_positive"
    assert data["resolution_comment"] == "Проверено, всё ОК"
    assert data["resolved_at"] is not None


@pytest.mark.asyncio
async def test_resolve_already_resolved(client: AsyncClient, supplier_with_invoices):
    inv_id = str(supplier_with_invoices["inv1"].id)
    create_resp = await client.post("/api/anomalies", json={
        "anomaly_type": "duplicate",
        "severity": "warning",
        "entity_type": "invoice",
        "entity_id": inv_id,
        "title": "Double resolve",
    })
    anomaly_id = create_resp.json()["id"]

    await client.post(f"/api/anomalies/{anomaly_id}/resolve", json={
        "resolution": "resolved", "comment": "Done",
    })
    resp = await client.post(f"/api/anomalies/{anomaly_id}/resolve", json={
        "resolution": "resolved", "comment": "Again",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_explain_anomaly(client: AsyncClient, supplier_with_invoices):
    inv_id = str(supplier_with_invoices["inv1"].id)
    create_resp = await client.post("/api/anomalies", json={
        "anomaly_type": "requisite_change",
        "severity": "critical",
        "entity_type": "invoice",
        "entity_id": inv_id,
        "title": "Смена реквизитов",
    })
    anomaly_id = create_resp.json()["id"]

    resp = await client.get(f"/api/anomalies/{anomaly_id}/explain")
    assert resp.status_code == 200
    data = resp.json()
    assert data["anomaly_type"] == "requisite_change"
    assert "реквизит" in data["explanation"].lower()
    assert len(data["suggested_actions"]) >= 2


@pytest.mark.asyncio
async def test_check_not_found(client: AsyncClient):
    resp = await client.post("/api/anomalies/check", json={
        "invoice_id": str(uuid.uuid4()),
    })
    assert resp.status_code == 404
