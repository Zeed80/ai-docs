"""Tests for Warehouse API — inventory, stock movements, receipts."""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.db.models import Document, DocumentStatus, Invoice, InvoiceStatus, InventoryItem


@pytest.fixture
async def bolt_item(db_session):
    item = InventoryItem(
        name="Болт М8х30",
        unit="шт",
        sku="BOLT-M8-30",
        current_qty=1000.0,
        min_qty=100.0,
        location="Стеллаж А-3",
    )
    db_session.add(item)
    await db_session.commit()
    return item


@pytest.fixture
async def low_stock_item(db_session):
    item = InventoryItem(
        name="Краска красная",
        unit="кг",
        sku="PAINT-RED",
        current_qty=5.0,
        min_qty=50.0,
        location="Склад 2",
    )
    db_session.add(item)
    await db_session.commit()
    return item


@pytest.fixture
async def invoice_for_receipt(db_session, bolt_item):
    from app.db.models import InvoiceLine

    doc = Document(
        file_name="wh-test.pdf",
        file_hash="whh001",
        file_size=512,
        mime_type="application/pdf",
        storage_path="w/1.pdf",
        status=DocumentStatus.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()

    inv = Invoice(
        document_id=doc.id,
        invoice_number="WH-001",
        currency="RUB",
        total_amount=5000.0,
        status=InvoiceStatus.needs_review,
        invoice_date=datetime.now(timezone.utc),
    )
    db_session.add(inv)
    await db_session.flush()

    line = InvoiceLine(
        invoice_id=inv.id,
        line_number=1,
        description="Болт М8х30",
        sku=bolt_item.sku,
        quantity=100.0,
        unit="шт",
        unit_price=5.5,
        amount=550.0,
    )
    db_session.add(line)
    await db_session.commit()
    return inv


# ── Inventory CRUD ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_inventory_item(client: AsyncClient):
    resp = await client.post("/api/warehouse/inventory", json={
        "name": "Гайка М8",
        "unit": "шт",
        "sku": "NUT-M8",
        "min_qty": 200.0,
        "location": "Стеллаж А-4",
        "current_qty": 500.0,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Гайка М8"
    assert data["unit"] == "шт"
    assert data["current_qty"] == 500.0


@pytest.mark.asyncio
async def test_list_inventory(client: AsyncClient, bolt_item):
    resp = await client.get("/api/warehouse/inventory")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    names = [i["name"] for i in data["items"]]
    assert "Болт М8х30" in names


@pytest.mark.asyncio
async def test_list_inventory_search(client: AsyncClient, bolt_item):
    resp = await client.get("/api/warehouse/inventory", params={"q": "Болт"})
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert "Болт" in item["name"] or "Болт" in (item.get("sku") or "")


@pytest.mark.asyncio
async def test_get_inventory_item(client: AsyncClient, bolt_item):
    resp = await client.get(f"/api/warehouse/inventory/{bolt_item.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(bolt_item.id)
    assert data["sku"] == "BOLT-M8-30"


@pytest.mark.asyncio
async def test_get_inventory_item_not_found(client: AsyncClient):
    resp = await client.get(f"/api/warehouse/inventory/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_inventory_item(client: AsyncClient, bolt_item):
    resp = await client.patch(f"/api/warehouse/inventory/{bolt_item.id}", json={
        "location": "Склад 1, полка 5",
        "min_qty": 200.0,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["location"] == "Склад 1, полка 5"
    assert data["min_qty"] == 200.0


@pytest.mark.asyncio
async def test_delete_inventory_item(client: AsyncClient):
    create_resp = await client.post("/api/warehouse/inventory", json={
        "name": "Временная позиция",
        "unit": "шт",
    })
    item_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/warehouse/inventory/{item_id}")
    assert resp.status_code == 200

    get_resp = await client.get(f"/api/warehouse/inventory/{item_id}")
    assert get_resp.status_code == 404


# ── Low stock ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_low_stock(client: AsyncClient, low_stock_item):
    resp = await client.get("/api/warehouse/inventory/low-stock")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    ids = [i["id"] for i in data["items"]]
    assert str(low_stock_item.id) in ids


# ── Stock movements ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_stock(client: AsyncClient, bolt_item):
    resp = await client.post(f"/api/warehouse/inventory/{bolt_item.id}/issue", json={
        "quantity": 50.0,
        "reason": "Выдача на производство",
        "performed_by": "Иванов И.И.",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["movement_type"] == "issue"
    assert data["quantity"] == -50.0  # issue recorded as negative
    assert data["balance_after"] == 950.0


@pytest.mark.asyncio
async def test_issue_more_than_available(client: AsyncClient, bolt_item):
    resp = await client.post(f"/api/warehouse/inventory/{bolt_item.id}/issue", json={
        "quantity": 99999.0,
        "reason": "Тест превышения",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_adjust_stock_positive(client: AsyncClient, bolt_item):
    resp = await client.post(f"/api/warehouse/inventory/{bolt_item.id}/adjust", json={
        "quantity": 100.0,
        "reason": "Поступление",
    })
    assert resp.status_code == 200
    assert resp.json()["movement_type"] == "adjustment"
    assert resp.json()["balance_after"] == 1100.0


@pytest.mark.asyncio
async def test_adjust_stock_negative(client: AsyncClient, bolt_item):
    resp = await client.post(f"/api/warehouse/inventory/{bolt_item.id}/adjust", json={
        "quantity": -200.0,
        "reason": "Списание",
    })
    assert resp.status_code == 200
    assert resp.json()["balance_after"] == 800.0


@pytest.mark.asyncio
async def test_list_movements(client: AsyncClient, bolt_item):
    await client.post(f"/api/warehouse/inventory/{bolt_item.id}/issue", json={
        "quantity": 10.0,
        "reason": "Test movement",
    })
    resp = await client.get("/api/warehouse/movements")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


# ── Receipts ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_receipt(client: AsyncClient, bolt_item, invoice_for_receipt):
    resp = await client.post("/api/warehouse/receipts", json={
        "invoice_id": str(invoice_for_receipt.id),
        "received_by": "Кладовщик Иванов",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["invoice_id"] == str(invoice_for_receipt.id)
    assert data["status"] == "draft"
    assert len(data["lines"]) == 1  # created from invoice line with matching SKU


@pytest.mark.asyncio
async def test_list_receipts(client: AsyncClient, bolt_item, invoice_for_receipt):
    await client.post("/api/warehouse/receipts", json={
        "invoice_id": str(invoice_for_receipt.id),
    })
    resp = await client.get("/api/warehouse/receipts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_confirm_receipt(client: AsyncClient, bolt_item, invoice_for_receipt):
    create_resp = await client.post("/api/warehouse/receipts", json={
        "invoice_id": str(invoice_for_receipt.id),
    })
    receipt_id = create_resp.json()["id"]

    resp = await client.post(f"/api/warehouse/receipts/{receipt_id}/confirm")
    assert resp.status_code == 200
    assert resp.json()["status"] in ("confirmed", "received")
