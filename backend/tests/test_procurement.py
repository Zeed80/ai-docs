"""Tests for Procurement API — purchase requests and supplier contracts."""

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient

from app.db.models import Party, PartyRole, Document, DocumentStatus


@pytest.fixture
async def supplier(db_session):
    party = Party(
        name='ООО "ТестПоставка"',
        inn="7712345678",
        role=PartyRole.supplier,
        contact_email="test@supplier.ru",
    )
    db_session.add(party)
    await db_session.commit()
    return party


@pytest.fixture
def purchase_items():
    return [
        {"name": "Болт М8х30", "qty": 500, "unit": "шт", "target_price": 5.5},
        {"name": "Гайка М8", "qty": 500, "unit": "шт", "target_price": 3.0},
    ]


# ── Purchase Requests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_purchase_request(client: AsyncClient, purchase_items):
    deadline = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    resp = await client.post("/api/purchase-requests", json={
        "title": "Закупка крепежа М8",
        "items": purchase_items,
        "deadline": deadline,
        "notes": "Тест-закупка",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Закупка крепежа М8"
    assert data["status"] == "draft"
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_list_purchase_requests(client: AsyncClient, purchase_items):
    await client.post("/api/purchase-requests", json={
        "title": "Список: запрос 1",
        "items": purchase_items,
    })
    await client.post("/api/purchase-requests", json={
        "title": "Список: запрос 2",
        "items": purchase_items,
    })
    resp = await client.get("/api/purchase-requests")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_get_purchase_request(client: AsyncClient, purchase_items):
    create_resp = await client.post("/api/purchase-requests", json={
        "title": "Для get-теста",
        "items": purchase_items,
    })
    assert create_resp.status_code == 201
    req_id = create_resp.json()["id"]

    resp = await client.get(f"/api/purchase-requests/{req_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == req_id
    assert resp.json()["title"] == "Для get-теста"


@pytest.mark.asyncio
async def test_get_purchase_request_not_found(client: AsyncClient):
    resp = await client.get(f"/api/purchase-requests/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_purchase_request(client: AsyncClient, purchase_items):
    create_resp = await client.post("/api/purchase-requests", json={
        "title": "Обновляемый запрос",
        "items": purchase_items,
    })
    req_id = create_resp.json()["id"]

    resp = await client.patch(f"/api/purchase-requests/{req_id}", json={
        "title": "Обновлённый заголовок",
        "notes": "Добавлены примечания",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Обновлённый заголовок"
    assert data["notes"] == "Добавлены примечания"


@pytest.mark.asyncio
async def test_update_status_to_approved(client: AsyncClient, purchase_items):
    create_resp = await client.post("/api/purchase-requests", json={
        "title": "Для статус-апдейта",
        "items": purchase_items,
    })
    req_id = create_resp.json()["id"]

    resp = await client.patch(f"/api/purchase-requests/{req_id}", json={
        "status": "approved",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_cancel_purchase_request(client: AsyncClient, purchase_items):
    create_resp = await client.post("/api/purchase-requests", json={
        "title": "Отменить меня",
        "items": purchase_items,
    })
    req_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/purchase-requests/{req_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Still retrievable but with cancelled status
    get_resp = await client.get(f"/api/purchase-requests/{req_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_send_rfq(client: AsyncClient, purchase_items, supplier):
    create_resp = await client.post("/api/purchase-requests", json={
        "title": "RFQ тест",
        "items": purchase_items,
    })
    req_id = create_resp.json()["id"]

    # supplier_ids is a JSON body (list of UUIDs)
    import json
    resp = await client.post(
        f"/api/purchase-requests/{req_id}/send-rfq",
        content=json.dumps([str(supplier.id)]),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "draft_email_ids" in data
    assert data["supplier_count"] == 1


@pytest.mark.asyncio
async def test_list_purchase_requests_filter_status(client: AsyncClient, purchase_items):
    create_resp = await client.post("/api/purchase-requests", json={
        "title": "Draft req",
        "items": purchase_items,
    })
    req_id = create_resp.json()["id"]
    await client.patch(f"/api/purchase-requests/{req_id}", json={"status": "approved"})

    resp = await client.get("/api/purchase-requests", params={"status": "draft"})
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["status"] == "draft"


# ── Supplier Contracts ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_supplier_contract(client: AsyncClient, supplier):
    start = datetime.now(timezone.utc).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    resp = await client.post("/api/supplier-contracts", json={
        "supplier_id": str(supplier.id),
        "contract_number": "ДКНТ-2026-001",
        "start_date": start,
        "end_date": end,
        "payment_terms": "30 дней",
        "currency": "RUB",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["contract_number"] == "ДКНТ-2026-001"
    assert data["status"] == "active"


@pytest.mark.asyncio
async def test_list_supplier_contracts(client: AsyncClient):
    resp = await client.get("/api/supplier-contracts")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data.get("items"), list)


@pytest.mark.asyncio
async def test_get_supplier_contract(client: AsyncClient, supplier):
    create_resp = await client.post("/api/supplier-contracts", json={
        "supplier_id": str(supplier.id),
        "contract_number": "GET-TEST-001",
    })
    assert create_resp.status_code == 201
    contract_id = create_resp.json()["id"]

    resp = await client.get(f"/api/supplier-contracts/{contract_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == contract_id


@pytest.mark.asyncio
async def test_update_supplier_contract_status(client: AsyncClient, supplier):
    create_resp = await client.post("/api/supplier-contracts", json={
        "supplier_id": str(supplier.id),
        "contract_number": "UPD-TEST-001",
    })
    contract_id = create_resp.json()["id"]

    resp = await client.patch(f"/api/supplier-contracts/{contract_id}", json={
        "status": "expired",
        "notes": "Истёк срок действия",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"
