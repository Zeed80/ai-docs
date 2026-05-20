"""Tests for Canonical Items API — normalization reference dictionary."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import (
    Document, DocumentStatus, Invoice, InvoiceStatus, InvoiceLine, CanonicalItem,
)
from datetime import datetime, timezone


@pytest.fixture
async def canonical_bolt(db_session):
    item = CanonicalItem(
        name="Болт М8х30 ГОСТ 7798-70",
        category="Крепёж",
        unit="шт",
        aliases=["Болт М8", "bolt M8x30"],
        is_confirmed=True,
        okpd2_code="25.94.11.110",
        gost="ГОСТ 7798-70",
    )
    db_session.add(item)
    await db_session.commit()
    return item


@pytest.fixture
async def invoice_with_line(db_session, canonical_bolt):
    doc = Document(
        file_name="norm-test.pdf",
        file_hash="normh001",
        file_size=512,
        mime_type="application/pdf",
        storage_path="n/1.pdf",
        status=DocumentStatus.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()

    inv = Invoice(
        document_id=doc.id,
        invoice_number="NORM-001",
        currency="RUB",
        total_amount=5500.0,
        status=InvoiceStatus.needs_review,
        invoice_date=datetime.now(timezone.utc),
    )
    db_session.add(inv)
    await db_session.flush()

    line = InvoiceLine(
        invoice_id=inv.id,
        line_number=1,
        description="Болт М8 x 30",
        quantity=500.0,
        unit="шт",
        unit_price=5.5,
        amount=2750.0,
    )
    db_session.add(line)
    await db_session.commit()
    return line


# ── CRUD ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_canonical_item(client: AsyncClient):
    resp = await client.post("/api/canonical", json={
        "name": "Гайка М8 ГОСТ 5915-70",
        "category": "Крепёж",
        "unit": "шт",
        "aliases": ["Гайка М8", "nut M8"],
        "okpd2_code": "25.94.12",
        "gost": "ГОСТ 5915-70",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Гайка М8 ГОСТ 5915-70"
    assert data["category"] == "Крепёж"
    assert data["is_confirmed"] is True


@pytest.mark.asyncio
async def test_list_canonical_items(client: AsyncClient, canonical_bolt):
    resp = await client.get("/api/canonical")
    assert resp.status_code == 200
    names = [i["name"] for i in resp.json()]
    assert "Болт М8х30 ГОСТ 7798-70" in names


@pytest.mark.asyncio
async def test_list_canonical_filter_category(client: AsyncClient, canonical_bolt):
    resp = await client.get("/api/canonical", params={"category": "Крепёж"})
    assert resp.status_code == 200
    for item in resp.json():
        assert item["category"] == "Крепёж"


@pytest.mark.asyncio
async def test_list_canonical_search(client: AsyncClient, canonical_bolt):
    resp = await client.get("/api/canonical", params={"q": "Болт"})
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) >= 1
    assert any("Болт" in i["name"] for i in results)


@pytest.mark.asyncio
async def test_get_canonical_item(client: AsyncClient, canonical_bolt):
    resp = await client.get(f"/api/canonical/{canonical_bolt.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(canonical_bolt.id)
    assert resp.json()["name"] == "Болт М8х30 ГОСТ 7798-70"


@pytest.mark.asyncio
async def test_get_canonical_item_not_found(client: AsyncClient):
    resp = await client.get(f"/api/canonical/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_canonical_item(client: AsyncClient, canonical_bolt):
    resp = await client.patch(f"/api/canonical/{canonical_bolt.id}", json={
        "description": "Шестигранный болт для металлоконструкций",
        "is_confirmed": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "Шестигранный болт для металлоконструкций"
    assert data["is_confirmed"] is True


@pytest.mark.asyncio
async def test_delete_canonical_item(client: AsyncClient):
    create_resp = await client.post("/api/canonical", json={
        "name": "Временная позиция для удаления",
    })
    item_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/canonical/{item_id}")
    assert resp.status_code == 204

    get_resp = await client.get(f"/api/canonical/{item_id}")
    assert get_resp.status_code == 404


# ── Suggest mapping ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suggest_mapping_by_description(client: AsyncClient, canonical_bolt):
    resp = await client.post("/api/canonical/suggest", json={
        "description": "Болт М8х30 для крепления",
        "limit": 5,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "matches" in data
    assert isinstance(data["matches"], list)
    # Bolt M8x30 should match our canonical item
    if data["matches"]:
        assert "canonical_item_id" in data["matches"][0]
        assert "score" in data["matches"][0]


@pytest.mark.asyncio
async def test_suggest_mapping_by_line_id(client: AsyncClient, invoice_with_line, canonical_bolt):
    resp = await client.post("/api/canonical/suggest", json={
        "invoice_line_id": str(invoice_with_line.id),
        "limit": 3,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "query" in data
    assert "matches" in data


# ── Confirm mapping ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_mapping(client: AsyncClient, invoice_with_line, canonical_bolt):
    resp = await client.post("/api/canonical/confirm", json={
        "invoice_line_id": str(invoice_with_line.id),
        "canonical_item_id": str(canonical_bolt.id),
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "invoice_line_id" in data or "status" in data or "canonical_item_id" in data


@pytest.mark.asyncio
async def test_confirm_mapping_invalid_line(client: AsyncClient, canonical_bolt):
    resp = await client.post("/api/canonical/confirm", json={
        "invoice_line_id": str(uuid.uuid4()),
        "canonical_item_id": str(canonical_bolt.id),
    })
    assert resp.status_code == 404
