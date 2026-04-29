"""Invoice API tests — invoice.list, invoice.get, invoice.validate, invoice.update, invoice.approve/reject"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentStatus, Invoice, InvoiceLine, InvoiceStatus, Party, PartyRole


async def _create_invoice(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper: create a Document + Invoice + 2 lines, return (doc_id, invoice_id)."""
    doc = Document(
        file_name="test_invoice.pdf",
        file_hash="abc123def456",
        file_size=1024,
        mime_type="application/pdf",
        storage_path="documents/ab/c1/abc123def456",
        status=DocumentStatus.needs_review,
    )
    db.add(doc)
    await db.flush()

    invoice = Invoice(
        document_id=doc.id,
        invoice_number="INV-001",
        currency="RUB",
        subtotal=10000.0,
        tax_amount=2000.0,
        total_amount=12000.0,
        status=InvoiceStatus.needs_review,
        overall_confidence=0.85,
    )
    db.add(invoice)
    await db.flush()

    line1 = InvoiceLine(
        invoice_id=invoice.id,
        line_number=1,
        description="Болты М10",
        quantity=100.0,
        unit="шт",
        unit_price=50.0,
        amount=5000.0,
        tax_rate=20.0,
        tax_amount=1000.0,
    )
    line2 = InvoiceLine(
        invoice_id=invoice.id,
        line_number=2,
        description="Гайки М10",
        quantity=100.0,
        unit="шт",
        unit_price=50.0,
        amount=5000.0,
        tax_rate=20.0,
        tax_amount=1000.0,
    )
    db.add_all([line1, line2])
    await db.commit()

    return doc.id, invoice.id


@pytest.mark.asyncio
async def test_list_invoices(client: AsyncClient, db_session: AsyncSession):
    """invoice.list — returns paginated list."""
    await _create_invoice(db_session)

    resp = await client.get("/api/invoices")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_get_invoice(client: AsyncClient, db_session: AsyncSession):
    """invoice.get — returns invoice with lines."""
    _, invoice_id = await _create_invoice(db_session)

    resp = await client.get(f"/api/invoices/{invoice_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["invoice_number"] == "INV-001"
    assert len(data["lines"]) == 2
    assert data["total_amount"] == 12000.0


@pytest.mark.asyncio
async def test_get_invoice_not_found(client: AsyncClient):
    """invoice.get — 404 for nonexistent invoice."""
    resp = await client.get("/api/invoices/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_validate_invoice(client: AsyncClient, db_session: AsyncSession):
    """invoice.validate — returns arithmetic validation results."""
    _, invoice_id = await _create_invoice(db_session)

    resp = await client.post(f"/api/invoices/{invoice_id}/validate")
    assert resp.status_code == 200
    data = resp.json()
    assert "is_valid" in data
    assert "errors" in data
    assert data["invoice_id"] == str(invoice_id)


@pytest.mark.asyncio
async def test_validate_invoice_with_errors(client: AsyncClient, db_session: AsyncSession):
    """invoice.validate — detects arithmetic errors."""
    doc = Document(
        file_name="bad_invoice.pdf",
        file_hash="badhash999",
        file_size=512,
        mime_type="application/pdf",
        storage_path="documents/ba/dh/badhash999",
        status=DocumentStatus.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()

    invoice = Invoice(
        document_id=doc.id,
        invoice_number="INV-BAD",
        currency="RUB",
        subtotal=100.0,
        tax_amount=20.0,
        total_amount=999.0,  # Wrong: 100 + 20 ≠ 999
        status=InvoiceStatus.needs_review,
    )
    db_session.add(invoice)
    await db_session.commit()

    resp = await client.post(f"/api/invoices/{invoice.id}/validate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is False
    assert len(data["errors"]) >= 1
    assert data["errors"][0]["field"] == "total_amount"


@pytest.mark.asyncio
async def test_update_invoice(client: AsyncClient, db_session: AsyncSession):
    """invoice.update — patch invoice fields."""
    _, invoice_id = await _create_invoice(db_session)

    resp = await client.patch(
        f"/api/invoices/{invoice_id}",
        json={"invoice_number": "INV-002", "total_amount": 15000.0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["invoice_number"] == "INV-002"
    assert data["total_amount"] == 15000.0


@pytest.mark.asyncio
async def test_approve_invoice(client: AsyncClient, db_session: AsyncSession):
    """invoice.approve — approve invoice."""
    _, invoice_id = await _create_invoice(db_session)

    resp = await client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"comment": "Всё верно"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_reject_invoice(client: AsyncClient, db_session: AsyncSession):
    """invoice.reject — reject invoice."""
    _, invoice_id = await _create_invoice(db_session)

    resp = await client.post(
        f"/api/invoices/{invoice_id}/reject",
        json={"reason": "Суммы не совпадают"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_approve_wrong_status(client: AsyncClient, db_session: AsyncSession):
    """invoice.approve — cannot approve already approved invoice."""
    _, invoice_id = await _create_invoice(db_session)

    # Approve first
    await client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"comment": "OK"},
    )

    # Try to approve again
    resp = await client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"comment": "Again"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_price_check(client: AsyncClient, db_session: AsyncSession):
    """invoice.compare_prices — compare with previous supplier invoices."""
    # Create supplier
    supplier = Party(name='ООО "АКМЕ"', inn="1234567890", role=PartyRole.supplier)
    db_session.add(supplier)
    await db_session.flush()

    # Create old invoice from same supplier
    old_doc = Document(
        file_name="old.pdf",
        file_hash="oldhash111",
        file_size=512,
        mime_type="application/pdf",
        storage_path="documents/ol/dh/oldhash111",
        status=DocumentStatus.approved,
    )
    db_session.add(old_doc)
    await db_session.flush()

    old_inv = Invoice(
        document_id=old_doc.id,
        invoice_number="OLD-001",
        currency="RUB",
        supplier_id=supplier.id,
        total_amount=10000.0,
        status=InvoiceStatus.approved,
    )
    db_session.add(old_inv)
    await db_session.flush()

    old_line = InvoiceLine(
        invoice_id=old_inv.id,
        line_number=1,
        description="Болты М10",
        quantity=100.0,
        unit="шт",
        unit_price=40.0,
        amount=4000.0,
    )
    db_session.add(old_line)

    # Create new invoice from same supplier
    new_doc = Document(
        file_name="new.pdf",
        file_hash="newhash222",
        file_size=512,
        mime_type="application/pdf",
        storage_path="documents/ne/wh/newhash222",
        status=DocumentStatus.needs_review,
    )
    db_session.add(new_doc)
    await db_session.flush()

    new_inv = Invoice(
        document_id=new_doc.id,
        invoice_number="NEW-001",
        currency="RUB",
        supplier_id=supplier.id,
        total_amount=12000.0,
        status=InvoiceStatus.needs_review,
    )
    db_session.add(new_inv)
    await db_session.flush()

    new_line = InvoiceLine(
        invoice_id=new_inv.id,
        line_number=1,
        description="Болты М10",
        quantity=100.0,
        unit="шт",
        unit_price=50.0,  # Price increased from 40 to 50
        amount=5000.0,
    )
    db_session.add(new_line)
    await db_session.commit()

    resp = await client.get(f"/api/invoices/{new_inv.id}/price-check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["supplier_name"] == 'ООО "АКМЕ"'
    assert data["previous_invoice_count"] == 1
    assert len(data["comparisons"]) == 1
    comp = data["comparisons"][0]
    assert comp["current_price"] == 50.0
    assert comp["previous_price"] == 40.0
    assert comp["price_change_pct"] == 25.0  # (50-40)/40 * 100


@pytest.mark.asyncio
async def test_price_check_no_supplier(client: AsyncClient, db_session: AsyncSession):
    """invoice.compare_prices — returns empty comparisons when no supplier."""
    _, invoice_id = await _create_invoice(db_session)

    resp = await client.get(f"/api/invoices/{invoice_id}/price-check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["previous_invoice_count"] == 0
    assert data["comparisons"] == []
