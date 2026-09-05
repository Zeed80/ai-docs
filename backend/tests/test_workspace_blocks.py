"""Tests for Workspace API — blocks CRUD and agent tools."""

import pytest
from httpx import AsyncClient

# ── Block CRUD ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_workspace_blocks_empty(client: AsyncClient):
    # Clear any existing blocks first
    await client.delete("/api/workspace/blocks")
    resp = await client.get("/api/workspace/blocks")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_verify_block_not_found(client: AsyncClient):
    resp = await client.post(
        "/api/workspace/agent/verify-block", json={"canvas_id": "agent:nonexistent-block-xyz"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is False
    assert data["canvas_id"] == "agent:nonexistent-block-xyz"


@pytest.mark.asyncio
async def test_delete_nonexistent_block(client: AsyncClient):
    resp = await client.delete("/api/workspace/blocks/nonexistent-canvas-id")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_clear_all_blocks(client: AsyncClient):
    resp = await client.delete("/api/workspace/blocks")
    assert resp.status_code == 200
    data = resp.json()
    assert "cleared" in data or "deleted" in data or isinstance(data, dict)


# ── Publish invoice table ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_invoice_table_empty(client: AsyncClient):
    resp = await client.post(
        "/api/workspace/agent/invoices/table",
        json={
            "canvas_id": "test:invoice-list",
            "limit": 10,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    assert data["canvas_id"] == "test:invoice-list"
    assert "total" in data
    assert "shown" in data
    assert "message" in data


@pytest.mark.asyncio
async def test_publish_invoice_table_appears_in_blocks(client: AsyncClient):
    await client.delete("/api/workspace/blocks")
    await client.post(
        "/api/workspace/agent/invoices/table",
        json={
            "canvas_id": "test:invoice-table-check",
        },
    )
    resp = await client.get("/api/workspace/blocks")
    assert resp.status_code == 200
    data = resp.json()
    block_ids = [item.get("id") for item in data["items"]]
    assert "test:invoice-table-check" in block_ids


@pytest.mark.asyncio
async def test_verify_block_after_publish(client: AsyncClient):
    await client.post(
        "/api/workspace/agent/invoices/table",
        json={
            "canvas_id": "test:verify-after-publish",
        },
    )
    resp = await client.post(
        "/api/workspace/agent/verify-block", json={"canvas_id": "test:verify-after-publish"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is True
    assert data["canvas_id"] == "test:verify-after-publish"


# ── Publish general block ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_general_block(client: AsyncClient):
    resp = await client.post(
        "/api/workspace/agent/generated/general",
        json={
            "canvas_id": "test:general-block",
            "block_type": "table",
            "title": "Тестовый блок",
            "columns": [{"key": "name", "header": "Имя", "type": "text"}],
            "rows": [{"name": "строка 1"}],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_delete_specific_block(client: AsyncClient):
    await client.post(
        "/api/workspace/agent/generated/general",
        json={
            "canvas_id": "test:block-to-delete",
            "block_type": "table",
            "title": "Удалить меня",
            "columns": [{"key": "id", "header": "ID", "type": "text"}],
            "rows": [],
        },
    )

    resp = await client.delete("/api/workspace/blocks/test:block-to-delete")
    assert resp.status_code == 200

    # Verify deleted
    verify_resp = await client.post(
        "/api/workspace/agent/verify-block", json={"canvas_id": "test:block-to-delete"}
    )
    assert verify_resp.json()["exists"] is False


@pytest.mark.asyncio
async def test_compare_table_data_publishes_diff(client: AsyncClient):
    await client.post(
        "/api/workspace/agent/generated/general",
        json={
            "canvas_id": "test:left-compare",
            "block_type": "table",
            "title": "Left",
            "columns": [
                {"key": "sku", "header": "SKU", "type": "text"},
                {"key": "price", "header": "Цена", "type": "number"},
            ],
            "rows": [{"sku": "A", "price": 10}, {"sku": "B", "price": 20}],
        },
    )
    await client.post(
        "/api/workspace/agent/generated/general",
        json={
            "canvas_id": "test:right-compare",
            "block_type": "table",
            "title": "Right",
            "columns": [
                {"key": "sku", "header": "SKU", "type": "text"},
                {"key": "price", "header": "Цена", "type": "number"},
            ],
            "rows": [{"sku": "A", "price": 12}, {"sku": "C", "price": 30}],
        },
    )

    resp = await client.post(
        "/api/workspace/agent/compare-table-data",
        json={
            "left_canvas_id": "test:left-compare",
            "right_canvas_id": "test:right-compare",
            "canvas_id": "test:compare-result",
            "key_fields": ["sku"],
            "compare_fields": ["price"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    assert data["total"] == 3

    blocks = (await client.get("/api/workspace/blocks")).json()["items"]
    block = next(b for b in blocks if b["id"] == "test:compare-result")
    statuses = {row["status"] for row in block["rows"]}
    assert {"changed", "removed", "added"} <= statuses


# ── Supplier filter on the invoice table ────────────────────────────────────


@pytest.mark.asyncio
async def test_invoice_table_filters_by_supplier(client: AsyncClient, db_session):
    """A request for ONE supplier's invoices must not publish everyone's.

    Live finding (2026-08-21): the agent did pass supplier_query, but this
    request schema had no such field — it was dropped silently and all 152
    invoices went to the desktop under the title "полный список".
    """
    from datetime import UTC, datetime

    from app.db.models import Document, DocumentStatus, Invoice, Party, PartyRole

    wanted = Party(name="ООО Нужный Поставщик", role=PartyRole.supplier)
    other = Party(name="ООО Другой Поставщик", role=PartyRole.supplier)
    db_session.add_all([wanted, other])
    await db_session.flush()

    def _doc(name: str, digest: str) -> Document:
        return Document(
            file_name=name,
            file_hash=digest,
            file_size=1024,
            mime_type="application/pdf",
            storage_path=f"w/{digest}.pdf",
            status=DocumentStatus.needs_review,
        )

    doc_wanted, doc_other = _doc("wanted.pdf", "wsfilter1"), _doc("other.pdf", "wsfilter2")
    db_session.add_all([doc_wanted, doc_other])
    await db_session.flush()

    now = datetime.now(UTC)
    db_session.add_all(
        [
            Invoice(
                document_id=doc_wanted.id,
                invoice_number="F-1",
                invoice_date=now,
                supplier_id=wanted.id,
                total_amount=100,
            ),
            Invoice(
                document_id=doc_other.id,
                invoice_number="O-1",
                invoice_date=now,
                supplier_id=other.id,
                total_amount=200,
            ),
        ]
    )
    await db_session.commit()

    resp = await client.post(
        "/api/workspace/agent/invoices/table",
        json={
            "canvas_id": "test:invoice-supplier-filter",
            "supplier_query": "Нужный Поставщик",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    # …and the applied filter is reported back, which is what the audit reads.
    assert data["filters"]["supplier_query"] == "Нужный Поставщик"

    block = (await client.get("/api/workspace/blocks/test:invoice-supplier-filter")).json()
    numbers = [row.get("invoice_number") for row in block["rows"]]
    assert numbers == ["F-1"]


@pytest.mark.asyncio
async def test_invoice_table_unknown_supplier_is_not_found(client: AsyncClient):
    """An unknown name must not read as "done, 0 invoices" (finding #5)."""
    resp = await client.post(
        "/api/workspace/agent/invoices/table",
        json={
            "canvas_id": "test:invoice-unknown-supplier",
            "supplier_query": "ООО Совсем Неизвестный Контрагент",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"


# ── Contract: every invoice-scoped table accepts the supplier filter ────────


def test_all_invoice_tables_accept_supplier_query():
    """A parameter a schema doesn't declare disappears silently — and the agent
    looks guilty. Twice on this deployment (2026-08-21) a per-supplier request
    published EVERY supplier's data because the table's request model had no
    supplier_query field. Keep the family consistent.
    """
    from app.api import workspace as ws

    invoice_table_requests = [
        ws.WorkspaceInvoiceTableRequest,
        ws.WorkspaceInvoiceItemsTableRequest,
        ws.WorkspaceInvoiceItemsGroupedTableRequest,
        ws.WorkspaceInvoiceItemsBySupplierTableRequest,
    ]
    missing = [
        model.__name__
        for model in invoice_table_requests
        if "supplier_query" not in model.model_fields
    ]
    assert not missing, f"invoice tables without a supplier filter: {missing}"


@pytest.mark.asyncio
async def test_items_by_supplier_table_honours_the_filter(client: AsyncClient, db_session):
    from datetime import UTC, datetime

    from app.db.models import (
        Document,
        DocumentStatus,
        Invoice,
        InvoiceLine,
        Party,
        PartyRole,
    )

    wanted = Party(name="ООО Фильтруемый", role=PartyRole.supplier)
    other = Party(name="ООО Посторонний", role=PartyRole.supplier)
    db_session.add_all([wanted, other])
    await db_session.flush()

    def _doc(digest: str) -> Document:
        return Document(
            file_name=f"{digest}.pdf",
            file_hash=digest,
            file_size=256,
            mime_type="application/pdf",
            storage_path=f"g/{digest}.pdf",
            status=DocumentStatus.needs_review,
        )

    d1, d2 = _doc("grpfilter1"), _doc("grpfilter2")
    db_session.add_all([d1, d2])
    await db_session.flush()

    now = datetime.now(UTC)
    inv1 = Invoice(
        document_id=d1.id,
        invoice_number="G-1",
        invoice_date=now,
        supplier_id=wanted.id,
        total_amount=100,
    )
    inv2 = Invoice(
        document_id=d2.id,
        invoice_number="G-2",
        invoice_date=now,
        supplier_id=other.id,
        total_amount=200,
    )
    db_session.add_all([inv1, inv2])
    await db_session.flush()
    db_session.add_all(
        [
            InvoiceLine(
                invoice_id=inv1.id, line_number=1, description="Фреза", quantity=1, amount=100
            ),
            InvoiceLine(
                invoice_id=inv2.id, line_number=1, description="Болт", quantity=1, amount=200
            ),
        ]
    )
    await db_session.commit()

    resp = await client.post(
        "/api/workspace/agent/invoices/items-by-supplier-table",
        json={"canvas_id": "test:by-supplier-filter", "supplier_query": "Фильтруемый"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["filters"]["supplier_query"] == "Фильтруемый"

    block = (await client.get("/api/workspace/blocks/test:by-supplier-filter")).json()
    suppliers = {row.get("supplier") for row in block["rows"]}
    assert suppliers == {"ООО Фильтруемый"}, f"filter ignored: {suppliers}"
