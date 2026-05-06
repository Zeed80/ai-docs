"""Tests for Tables & Export API."""


import pytest
from httpx import AsyncClient

from app.db.models import (
    Document,
    DocumentStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Party,
    PartyRole,
)


@pytest.fixture
async def sample_invoice(db_session):
    """Create a sample invoice with lines and supplier for table tests."""
    doc = Document(
        file_name="test-table.pdf",
        file_hash="hash_table_001",
        file_size=5000,
        mime_type="application/pdf",
        storage_path="test/table.pdf",
        status=DocumentStatus.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()

    supplier = Party(name="ТестПоставщик", inn="1234567890", role=PartyRole.supplier)
    db_session.add(supplier)
    await db_session.flush()

    invoice = Invoice(
        document_id=doc.id,
        invoice_number="T-100",
        currency="RUB",
        total_amount=10000.0,
        tax_amount=2000.0,
        subtotal=8000.0,
        status=InvoiceStatus.needs_review,
        supplier_id=supplier.id,
        overall_confidence=0.85,
    )
    db_session.add(invoice)
    await db_session.flush()

    line = InvoiceLine(
        invoice_id=invoice.id,
        line_number=1,
        description="Деталь А",
        quantity=10,
        unit="шт",
        unit_price=800.0,
        amount=8000.0,
        tax_rate=20,
        tax_amount=1600.0,
    )
    db_session.add(line)
    await db_session.commit()
    return invoice


@pytest.mark.asyncio
async def test_table_query_invoices(client: AsyncClient, sample_invoice):
    resp = await client.post("/api/tables/query", json={
        "table": "invoices",
        "search": "T-100",
        "limit": 10,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert len(data["rows"]) >= 1
    assert len(data["columns"]) > 0

    row = data["rows"][0]
    assert row["data"]["invoice_number"] == "T-100"
    assert row["data"]["supplier_name"] == "ТестПоставщик"


@pytest.mark.asyncio
async def test_table_query_with_filter(client: AsyncClient, sample_invoice):
    resp = await client.post("/api/tables/query", json={
        "table": "invoices",
        "filters": [{"column": "status", "operator": "eq", "value": "needs_review"}],
    })
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


@pytest.mark.asyncio
async def test_table_query_documents(client: AsyncClient, sample_invoice):
    resp = await client.post("/api/tables/query", json={
        "table": "documents",
        "limit": 10,
    })
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


@pytest.mark.asyncio
async def test_export_xlsx(client: AsyncClient, sample_invoice):
    resp = await client.post("/api/tables/export", json={
        "table": "invoices",
        "format": "xlsx",
    })
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.headers["content-type"]
    assert len(resp.content) > 100  # non-trivial file


@pytest.mark.asyncio
async def test_export_csv(client: AsyncClient, sample_invoice):
    resp = await client.post("/api/tables/export", json={
        "table": "invoices",
        "format": "csv",
    })
    assert resp.status_code == 200
    text = resp.content.decode("utf-8-sig")
    assert "Номер счёта" in text
    assert "T-100" in text
    assert "10 000,00" in text
    assert ";" in text.splitlines()[0]


@pytest.mark.asyncio
async def test_export_1c(client: AsyncClient, sample_invoice):
    resp = await client.post("/api/tables/export-1c", json={
        "invoice_ids": [str(sample_invoice.id)],
    })
    assert resp.status_code == 200
    xml = resp.content.decode("utf-8")
    assert "КоммерческаяИнформация" in xml
    assert "ВерсияСхемы" in xml
    assert "T-100" in xml or "Номер" in xml
    assert "ТестПоставщик" in xml
    assert "Деталь А" in xml


@pytest.mark.asyncio
async def test_workspace_invoice_items_by_supplier_table(client: AsyncClient, sample_invoice):
    resp = await client.post(
        "/api/workspace/agent/invoices/items-by-supplier-table",
        json={"limit": 100},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    assert data["shown"] >= 1
    assert "поставщик" in data["message"].lower()

    block_resp = await client.get("/api/workspace/blocks/agent%3Ainvoice-items-by-supplier")
    assert block_resp.status_code == 200
    block = block_resp.json()
    assert block["source"] == "workspace.invoice_items_by_supplier_table"
    assert [column["key"] for column in block["columns"]] == [
        "index",
        "supplier",
        "invoice_count",
        "items",
        "total_amount",
    ]
    row = next(row for row in block["rows"] if row["supplier"] == "ТестПоставщик")
    assert "Деталь А" in row["items"]
    assert "счет T-100" in row["items"]


@pytest.mark.asyncio
async def test_inline_edit(client: AsyncClient, sample_invoice):
    resp = await client.post("/api/tables/inline-edit", json={
        "entity_id": str(sample_invoice.id),
        "field": "invoice_number",
        "value": "T-200",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["old_value"] == "T-100"
    assert data["new_value"] == "T-200"


@pytest.mark.asyncio
async def test_inline_edit_numeric(client: AsyncClient, sample_invoice):
    resp = await client.post("/api/tables/inline-edit", json={
        "entity_id": str(sample_invoice.id),
        "field": "total_amount",
        "value": 15000.0,
    })
    assert resp.status_code == 200
    assert resp.json()["new_value"] == 15000.0


@pytest.mark.asyncio
async def test_inline_edit_forbidden_field(client: AsyncClient, sample_invoice):
    resp = await client.post("/api/tables/inline-edit", json={
        "entity_id": str(sample_invoice.id),
        "field": "status",
        "value": "approved",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_batch_approve(client: AsyncClient, sample_invoice):
    resp = await client.post("/api/tables/batch", json={
        "action": "approve",
        "entity_ids": [str(sample_invoice.id)],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["succeeded"] == 1
    assert data["failed"] == 0


@pytest.mark.asyncio
async def test_batch_reject_wrong_status(client: AsyncClient, sample_invoice):
    # First approve
    await client.post("/api/tables/batch", json={
        "action": "approve",
        "entity_ids": [str(sample_invoice.id)],
    })
    # Then try to reject — should fail (already approved)
    resp = await client.post("/api/tables/batch", json={
        "action": "reject",
        "entity_ids": [str(sample_invoice.id)],
        "reason": "test",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["succeeded"] == 0
    assert data["failed"] == 1


@pytest.mark.asyncio
async def test_saved_view_crud(client: AsyncClient):
    # Create
    resp = await client.post("/api/tables/views", json={
        "name": "Мой вид",
        "table": "invoices",
        "filters": [{"column": "status", "operator": "eq", "value": "needs_review"}],
        "sort": [{"column": "total_amount", "direction": "desc"}],
    })
    assert resp.status_code == 200
    view = resp.json()
    assert view["name"] == "Мой вид"
    view_id = view["id"]

    # List
    resp = await client.get("/api/tables/views?table=invoices")
    assert resp.status_code == 200
    views = resp.json()
    assert any(v["id"] == view_id for v in views)

    # Delete
    resp = await client.delete(f"/api/tables/views/{view_id}")
    assert resp.status_code == 200
