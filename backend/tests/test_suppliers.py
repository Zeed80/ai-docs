"""Tests for Supplier API."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import (
    Document, DocumentStatus, Invoice, InvoiceLine, InvoiceStatus,
    Party, PartyRole, SupplierProfile,
)


@pytest.fixture
async def supplier(db_session):
    party = Party(
        name="ООО Тест",
        inn="7719826705",
        kpp="771901001",
        role=PartyRole.supplier,
        contact_email="test@supplier.ru",
        bank_account="40702810038000197568",
        bank_bik="044525225",
        address="г. Москва, ул. Тестовая, 1",
    )
    db_session.add(party)
    await db_session.flush()

    profile = SupplierProfile(
        party_id=party.id,
        total_invoices=5,
        total_amount=50000.0,
    )
    db_session.add(profile)
    await db_session.flush()

    # Create 2 invoices with lines for price history
    doc1 = Document(
        file_name="inv1.pdf", file_hash="h1", file_size=100,
        mime_type="application/pdf", storage_path="t/1.pdf", status=DocumentStatus.approved,
    )
    doc2 = Document(
        file_name="inv2.pdf", file_hash="h2", file_size=200,
        mime_type="application/pdf", storage_path="t/2.pdf", status=DocumentStatus.approved,
    )
    db_session.add_all([doc1, doc2])
    await db_session.flush()

    inv1 = Invoice(
        document_id=doc1.id, invoice_number="S-001", currency="RUB",
        total_amount=10000.0, status=InvoiceStatus.approved, supplier_id=party.id,
    )
    inv2 = Invoice(
        document_id=doc2.id, invoice_number="S-002", currency="RUB",
        total_amount=12000.0, status=InvoiceStatus.needs_review, supplier_id=party.id,
    )
    db_session.add_all([inv1, inv2])
    await db_session.flush()

    line1 = InvoiceLine(
        invoice_id=inv1.id, line_number=1, description="Болт М8",
        quantity=100, unit="шт", unit_price=50.0, amount=5000.0,
    )
    line2 = InvoiceLine(
        invoice_id=inv1.id, line_number=2, description="Гайка М8",
        quantity=100, unit="шт", unit_price=30.0, amount=3000.0,
    )
    line3 = InvoiceLine(
        invoice_id=inv2.id, line_number=1, description="Болт М8",
        quantity=100, unit="шт", unit_price=55.0, amount=5500.0,
    )
    db_session.add_all([line1, line2, line3])
    await db_session.commit()
    return party


@pytest.mark.asyncio
async def test_get_supplier(client: AsyncClient, supplier):
    resp = await client.get(f"/api/suppliers/{supplier.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "ООО Тест"
    assert data["inn"] == "7719826705"
    assert data["profile"] is not None
    assert data["profile"]["total_invoices"] == 5
    assert data["recent_invoices_count"] == 2


@pytest.mark.asyncio
async def test_search_suppliers(client: AsyncClient, supplier):
    resp = await client.post("/api/suppliers/search", json={"query": "Тест"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(r["name"] == "ООО Тест" for r in data["results"])


@pytest.mark.asyncio
async def test_search_by_inn(client: AsyncClient, supplier):
    resp = await client.post("/api/suppliers/search", json={"query": "7719826705"})
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


@pytest.mark.asyncio
async def test_price_history(client: AsyncClient, supplier):
    resp = await client.get(f"/api/suppliers/{supplier.id}/price-history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["supplier_name"] == "ООО Тест"
    assert data["total_items"] >= 1

    bolt = next((i for i in data["items"] if "Болт" in i["description"]), None)
    assert bolt is not None
    assert len(bolt["points"]) == 2
    assert bolt["points"][-1]["price"] == 55.0
    assert bolt["trend"] == "up"


@pytest.mark.asyncio
async def test_check_requisites(client: AsyncClient, supplier):
    resp = await client.post(f"/api/suppliers/{supplier.id}/check-requisites")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is True
    # All fields filled
    ok_fields = [c["field"] for c in data["checks"] if c["status"] == "ok"]
    assert "inn" in ok_fields
    assert "bank_account" in ok_fields


@pytest.mark.asyncio
async def test_trust_score(client: AsyncClient, supplier):
    resp = await client.get(f"/api/suppliers/{supplier.id}/trust-score")
    assert resp.status_code == 200
    data = resp.json()
    assert 0 <= data["trust_score"] <= 1.0
    assert len(data["breakdown"]) == 4
    assert data["recommendation"] is not None


@pytest.mark.asyncio
async def test_alerts(client: AsyncClient, supplier):
    resp = await client.get(f"/api/suppliers/{supplier.id}/alerts")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["alerts"], list)
    # May have price_increase or missing_docs alerts depending on fixture state
    assert data["total"] >= 0


@pytest.mark.asyncio
async def test_update_supplier(client: AsyncClient, supplier):
    resp = await client.patch(f"/api/suppliers/{supplier.id}", json={
        "contact_phone": "+7 999 123 4567",
    })
    assert resp.status_code == 200
    assert resp.json()["contact_phone"] == "+7 999 123 4567"


@pytest.mark.asyncio
async def test_list_suppliers(client: AsyncClient, supplier):
    resp = await client.get("/api/suppliers")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
