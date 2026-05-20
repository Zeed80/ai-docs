"""Tests for Tool Catalog API — suppliers and catalog entries CRUD."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import ToolSupplier, ToolCatalogEntry, ToolTypeEnum


@pytest.fixture
async def supplier(db_session):
    s = ToolSupplier(
        name="Инструмент-Плюс",
        website="https://tool-plus.ru",
        country="RU",
        is_active=True,
    )
    db_session.add(s)
    await db_session.commit()
    return s


@pytest.fixture
async def catalog_entry(db_session, supplier):
    entry = ToolCatalogEntry(
        supplier_id=supplier.id,
        part_number="DRL-8MM-HSS",
        tool_type=ToolTypeEnum.drill,
        name="Сверло Ø8мм HSS",
        diameter_mm=8.0,
        length_mm=75.0,
        material="HSS",
        price_value=350.0,
        price_currency="RUB",
    )
    db_session.add(entry)
    await db_session.commit()
    return entry


# ── Suppliers ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_supplier(client: AsyncClient):
    resp = await client.post("/api/tool-catalog/suppliers", json={
        "name": "Новый поставщик инструмента",
        "country": "RU",
        "is_active": True,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Новый поставщик инструмента"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_list_suppliers(client: AsyncClient, supplier):
    resp = await client.get("/api/tool-catalog/suppliers", params={"active_only": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    names = [s["name"] for s in data["items"]]
    assert "Инструмент-Плюс" in names


@pytest.mark.asyncio
async def test_list_suppliers_active_only(client: AsyncClient, supplier):
    resp = await client.get("/api/tool-catalog/suppliers", params={"active_only": True})
    assert resp.status_code == 200
    for s in resp.json()["items"]:
        assert s["is_active"] is True


@pytest.mark.asyncio
async def test_get_supplier(client: AsyncClient, supplier):
    resp = await client.get(f"/api/tool-catalog/suppliers/{supplier.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(supplier.id)
    assert data["name"] == "Инструмент-Плюс"


@pytest.mark.asyncio
async def test_get_supplier_not_found(client: AsyncClient):
    resp = await client.get(f"/api/tool-catalog/suppliers/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_supplier(client: AsyncClient, supplier):
    resp = await client.patch(f"/api/tool-catalog/suppliers/{supplier.id}", json={
        "website": "https://new-tool-plus.ru",
        "notes": "Обновлён адрес сайта",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["website"] == "https://new-tool-plus.ru"
    assert data["notes"] == "Обновлён адрес сайта"


@pytest.mark.asyncio
async def test_delete_supplier(client: AsyncClient):
    create_resp = await client.post("/api/tool-catalog/suppliers", json={
        "name": "Временный поставщик",
    })
    supplier_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/tool-catalog/suppliers/{supplier_id}")
    assert resp.status_code in (200, 204)


# ── Catalog entries ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_catalog_entry(client: AsyncClient, supplier):
    resp = await client.post("/api/tool-catalog/entries", json={
        "supplier_id": str(supplier.id),
        "part_number": "EM-6MM-TiN",
        "tool_type": "endmill",
        "name": "Фреза концевая Ø6мм TiN",
        "diameter_mm": 6.0,
        "length_mm": 52.0,
        "material": "HSS-Co",
        "coating": "TiN",
        "price_value": 1200.0,
        "price_currency": "RUB",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["tool_type"] == "endmill"
    assert data["name"] == "Фреза концевая Ø6мм TiN"
    assert data["diameter_mm"] == 6.0


@pytest.mark.asyncio
async def test_search_catalog_entries(client: AsyncClient, catalog_entry):
    resp = await client.get("/api/tool-catalog/search", params={"semantic": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    names = [e["name"] for e in data["items"]]
    assert "Сверло Ø8мм HSS" in names


@pytest.mark.asyncio
async def test_search_catalog_filter_by_type(client: AsyncClient, catalog_entry):
    resp = await client.get("/api/tool-catalog/search", params={"tool_type": "drill", "semantic": False})
    assert resp.status_code == 200
    for entry in resp.json()["items"]:
        assert entry["tool_type"] == "drill"


@pytest.mark.asyncio
async def test_get_catalog_entry(client: AsyncClient, catalog_entry):
    resp = await client.get(f"/api/tool-catalog/entries/{catalog_entry.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["part_number"] == "DRL-8MM-HSS"
    assert data["diameter_mm"] == 8.0


@pytest.mark.asyncio
async def test_get_catalog_entry_not_found(client: AsyncClient):
    resp = await client.get(f"/api/tool-catalog/entries/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_catalog_entry(client: AsyncClient, catalog_entry):
    resp = await client.patch(f"/api/tool-catalog/entries/{catalog_entry.id}", json={
        "price_value": 380.0,
        "notes": "Новая цена 2026",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["price_value"] == 380.0


@pytest.mark.asyncio
async def test_delete_catalog_entry(client: AsyncClient, catalog_entry):
    resp = await client.delete(f"/api/tool-catalog/entries/{catalog_entry.id}")
    assert resp.status_code in (200, 204)
