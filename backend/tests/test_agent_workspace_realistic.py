"""Realistic agent table / desktop tests on populated invoice data.

Exercises the tools the agent ("Света") uses to build tables and put them on
the workspace desktop, but with *real* multi-invoice data and content
assertions (existing table/workspace tests use minimal/empty fixtures):

* ``/api/tables/query`` — invoice table returns correct rows/values.
* ``/api/tables/export`` — XLSX export actually contains the invoice data.
* ``/api/workspace/agent/invoices/table`` → ``/api/workspace/blocks`` — the
  agent publishes a table block to the desktop and its rows reflect the DB.
* ``/api/workspace/agent/invoice-items/table`` — line-item table on the desktop.

Uses the in-process API + test Postgres (conftest). Workspace blocks degrade to
the in-memory fallback when Redis is absent, so this runs without Redis.
"""

from __future__ import annotations

import io

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


async def _seed_invoice(db, *, number, supplier_name, inn, total, lines):
    doc = Document(
        file_name=f"{number}.pdf",
        file_hash=f"hash_{number}",
        file_size=4096,
        mime_type="application/pdf",
        storage_path=f"documents/{number}.pdf",
        status=DocumentStatus.approved,
    )
    db.add(doc)
    await db.flush()
    supplier = Party(name=supplier_name, inn=inn, role=PartyRole.supplier)
    db.add(supplier)
    await db.flush()
    inv = Invoice(
        document_id=doc.id,
        invoice_number=number,
        currency="RUB",
        subtotal=total / 1.2,
        tax_amount=total - total / 1.2,
        total_amount=total,
        status=InvoiceStatus.approved,
        supplier_id=supplier.id,
        overall_confidence=0.99,
    )
    db.add(inv)
    await db.flush()
    for i, (desc, sku, qty, price) in enumerate(lines, start=1):
        db.add(InvoiceLine(
            invoice_id=inv.id, line_number=i, description=desc, sku=sku,
            quantity=qty, unit="шт", unit_price=price, amount=qty * price,
            tax_rate=0.2, tax_amount=qty * price * 0.2,
        ))
    await db.commit()
    return inv


@pytest.fixture
async def invoices(db_session):
    await _seed_invoice(
        db_session, number="СЧ-1001", supplier_name='ООО "НВС Компани"',
        inn="7707083893", total=22800.0,
        lines=[("Фреза концевая Ø10", "A-1", 10, 1500.0), ("Сверло Ø5", "B-2", 5, 800.0)],
    )
    await _seed_invoice(
        db_session, number="СЧ-1002", supplier_name='ООО "Графит-Гарант"',
        inn="7447286384", total=12000.0,
        lines=[("Графитовый блок", "G-7", 4, 2500.0)],
    )
    return 2


@pytest.fixture(autouse=True)
async def _clear_desktop(client: AsyncClient):
    """Start each test with an empty workspace desktop (in-memory fallback)."""
    await client.delete("/api/workspace/blocks")
    yield


# ── Tables ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_table_query_returns_real_invoices(client: AsyncClient, invoices):
    resp = await client.post("/api/tables/query", json={"table": "invoices", "limit": 50})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2
    by_number = {r["data"]["invoice_number"]: r["data"] for r in data["rows"]}
    assert "СЧ-1001" in by_number and "СЧ-1002" in by_number
    assert by_number["СЧ-1001"]["supplier_name"] == 'ООО "НВС Компани"'
    assert float(by_number["СЧ-1001"]["total_amount"]) == 22800.0
    # line_count column reflects the seeded lines
    assert by_number["СЧ-1001"]["line_count"] == 2


@pytest.mark.asyncio
async def test_agent_table_search_by_number(client: AsyncClient, invoices):
    resp = await client.post("/api/tables/query", json={
        "table": "invoices", "search": "1002",
    })
    assert resp.status_code == 200
    data = resp.json()
    numbers = {r["data"]["invoice_number"] for r in data["rows"]}
    assert "СЧ-1002" in numbers
    assert "СЧ-1001" not in numbers


@pytest.mark.asyncio
async def test_agent_export_xlsx_contains_invoice_data(client: AsyncClient, invoices):
    resp = await client.post("/api/tables/export", json={"table": "invoices", "format": "xlsx"})
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.headers["content-type"]

    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.load_workbook(io.BytesIO(resp.content))
    ws = wb.active
    cells = {str(c.value) for row in ws.iter_rows() for c in row if c.value is not None}
    assert "СЧ-1001" in cells
    assert "СЧ-1002" in cells
    assert any("НВС" in c for c in cells)


# ── Desktop (workspace blocks) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_publishes_invoice_table_to_desktop(client: AsyncClient, invoices):
    pub = await client.post("/api/workspace/agent/invoices/table", json={
        "canvas_id": "agent:invoice-list",
    })
    assert pub.status_code == 200
    body = pub.json()
    assert body["status"] == "published"
    assert body["total"] >= 2

    blocks = (await client.get("/api/workspace/blocks")).json()
    block = next(b for b in blocks["items"] if b["id"] == "agent:invoice-list")
    assert block["type"] == "table"
    assert len(block["rows"]) >= 2
    numbers = {r.get("invoice_number") for r in block["rows"]}
    suppliers = {r.get("supplier") for r in block["rows"]}
    assert {"СЧ-1001", "СЧ-1002"} <= numbers
    assert 'ООО "НВС Компани"' in suppliers

    # The block is verifiable via the agent's verify-block tool.
    verify = await client.post("/api/workspace/agent/verify-block", json={
        "canvas_id": "agent:invoice-list",
    })
    assert verify.status_code == 200
    v = verify.json()
    assert v["exists"] is True
    assert v["row_count"] >= 2


@pytest.mark.asyncio
async def test_agent_publishes_invoice_items_to_desktop(client: AsyncClient, invoices):
    pub = await client.post("/api/workspace/agent/invoices/items-table", json={
        "canvas_id": "agent:invoice-items",
    })
    assert pub.status_code == 200
    assert pub.json()["status"] == "published"

    blocks = (await client.get("/api/workspace/blocks")).json()
    block = next(b for b in blocks["items"] if b["id"] == "agent:invoice-items")
    # Line items from both invoices are present (3 lines total)
    assert len(block["rows"]) >= 3
    text = " ".join(str(r) for r in block["rows"])
    assert "Фреза концевая Ø10" in text
    assert "Графитовый блок" in text
