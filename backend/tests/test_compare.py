"""Tests for Compare КП API."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import (
    Document, DocumentStatus, Invoice, InvoiceLine, InvoiceStatus,
    Party, PartyRole,
)


@pytest.fixture
async def two_suppliers_with_invoices(db_session):
    """Two suppliers with invoices containing overlapping line items."""
    sup1 = Party(name="ООО Альфа", inn="1111111111", role=PartyRole.supplier)
    sup2 = Party(name="ООО Бета", inn="2222222222", role=PartyRole.supplier)
    db_session.add_all([sup1, sup2])
    await db_session.flush()

    doc1 = Document(
        file_name="ko1.pdf", file_hash="kh1", file_size=100,
        mime_type="application/pdf", storage_path="k/1.pdf",
        status=DocumentStatus.approved,
    )
    doc2 = Document(
        file_name="ko2.pdf", file_hash="kh2", file_size=200,
        mime_type="application/pdf", storage_path="k/2.pdf",
        status=DocumentStatus.approved,
    )
    db_session.add_all([doc1, doc2])
    await db_session.flush()

    inv1 = Invoice(
        document_id=doc1.id, invoice_number="KP-001", currency="RUB",
        total_amount=15000.0, status=InvoiceStatus.needs_review,
        supplier_id=sup1.id,
    )
    inv2 = Invoice(
        document_id=doc2.id, invoice_number="KP-002", currency="RUB",
        total_amount=13000.0, status=InvoiceStatus.needs_review,
        supplier_id=sup2.id,
    )
    db_session.add_all([inv1, inv2])
    await db_session.flush()

    # Overlapping items: both have "Болт М12" and "Гайка М12"
    lines = [
        InvoiceLine(invoice_id=inv1.id, line_number=1, description="Болт М12",
                    quantity=100, unit="шт", unit_price=80.0, amount=8000.0),
        InvoiceLine(invoice_id=inv1.id, line_number=2, description="Гайка М12",
                    quantity=200, unit="шт", unit_price=35.0, amount=7000.0),
        InvoiceLine(invoice_id=inv2.id, line_number=1, description="Болт М12",
                    quantity=100, unit="шт", unit_price=70.0, amount=7000.0),
        InvoiceLine(invoice_id=inv2.id, line_number=2, description="Гайка М12",
                    quantity=200, unit="шт", unit_price=30.0, amount=6000.0),
    ]
    db_session.add_all(lines)
    await db_session.commit()
    return {"sup1": sup1, "sup2": sup2, "inv1": inv1, "inv2": inv2}


@pytest.mark.asyncio
async def test_create_session(client: AsyncClient, two_suppliers_with_invoices):
    data = two_suppliers_with_invoices
    resp = await client.post("/api/compare", json={
        "name": "Тест сравнения",
        "invoice_ids": [str(data["inv1"].id), str(data["inv2"].id)],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "draft"
    assert body["name"] == "Тест сравнения"
    assert len(body["invoice_ids"]) == 2


@pytest.mark.asyncio
async def test_create_session_requires_two(client: AsyncClient, two_suppliers_with_invoices):
    data = two_suppliers_with_invoices
    resp = await client.post("/api/compare", json={
        "name": "Один",
        "invoice_ids": [str(data["inv1"].id)],
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_align_and_summary(client: AsyncClient, two_suppliers_with_invoices):
    data = two_suppliers_with_invoices
    # Create session
    create_resp = await client.post("/api/compare", json={
        "name": "Alignment test",
        "invoice_ids": [str(data["inv1"].id), str(data["inv2"].id)],
    })
    session_id = create_resp.json()["id"]

    # Align
    align_resp = await client.post(f"/api/compare/{session_id}/align")
    assert align_resp.status_code == 200
    align_data = align_resp.json()
    assert len(align_data["items"]) == 2  # Болт М12 + Гайка М12

    # Check both suppliers present in each aligned item
    for item in align_data["items"]:
        assert len(item["items"]) == 2

    # Session should be "aligned" now
    get_resp = await client.get(f"/api/compare/{session_id}")
    assert get_resp.json()["status"] == "aligned"

    # Summary
    summary_resp = await client.get(f"/api/compare/{session_id}/summary")
    assert summary_resp.status_code == 200
    summary = summary_resp.json()
    assert summary["total_items"] == 2
    assert len(summary["suppliers"]) == 2
    assert summary["cheapest_total"] is not None
    assert summary["recommendation"] is not None
    # Бета is cheaper (13000 vs 15000)
    assert "Бета" in summary["cheapest_total"]["name"]


@pytest.mark.asyncio
async def test_decide(client: AsyncClient, two_suppliers_with_invoices):
    data = two_suppliers_with_invoices
    create_resp = await client.post("/api/compare", json={
        "name": "Decision test",
        "invoice_ids": [str(data["inv1"].id), str(data["inv2"].id)],
    })
    session_id = create_resp.json()["id"]

    # Align first
    await client.post(f"/api/compare/{session_id}/align")

    # Decide
    resp = await client.post(f"/api/compare/{session_id}/decide", json={
        "chosen_supplier_id": str(data["sup2"].id),
        "reasoning": "Дешевле на 2000 ₽",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "decided"
    assert resp.json()["decided_by"] == "user"
    assert resp.json()["decided_at"] is not None


@pytest.mark.asyncio
async def test_decide_twice_fails(client: AsyncClient, two_suppliers_with_invoices):
    data = two_suppliers_with_invoices
    create_resp = await client.post("/api/compare", json={
        "name": "Double decide",
        "invoice_ids": [str(data["inv1"].id), str(data["inv2"].id)],
    })
    session_id = create_resp.json()["id"]
    await client.post(f"/api/compare/{session_id}/align")

    await client.post(f"/api/compare/{session_id}/decide", json={
        "chosen_supplier_id": str(data["sup1"].id),
    })
    resp = await client.post(f"/api/compare/{session_id}/decide", json={
        "chosen_supplier_id": str(data["sup2"].id),
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_sessions(client: AsyncClient, two_suppliers_with_invoices):
    data = two_suppliers_with_invoices
    await client.post("/api/compare", json={
        "name": "List test",
        "invoice_ids": [str(data["inv1"].id), str(data["inv2"].id)],
    })
    resp = await client.get("/api/compare")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
