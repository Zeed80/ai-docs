"""Tests for BOM API — Bill of Materials CRUD, approve, stock check."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import BOM, BOMLine, InventoryItem


@pytest.fixture
async def bom(db_session):
    b = BOM(
        product_name="Стол офисный",
        product_code="DSK-001",
        version="1.0",
        status="draft",
    )
    db_session.add(b)
    await db_session.commit()
    return b


@pytest.fixture
async def bom_with_lines(db_session):
    b = BOM(
        product_name="Шкаф металлический",
        product_code="CAB-001",
        version="2.0",
        status="draft",
    )
    db_session.add(b)
    await db_session.flush()

    line = BOMLine(
        bom_id=b.id,
        line_number=1,
        description="Лист стальной 2мм",
        quantity=4.0,
        unit="шт",
    )
    db_session.add(line)
    await db_session.commit()
    return b


@pytest.fixture
async def shelf_item(db_session):
    item = InventoryItem(
        name="Лист стальной 2мм",
        unit="шт",
        sku="STEEL-2MM",
        current_qty=10.0,
        min_qty=2.0,
    )
    db_session.add(item)
    await db_session.commit()
    return item


# ── BOM CRUD ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_bom(client: AsyncClient):
    resp = await client.post("/api/boms", json={
        "product_name": "Тумба офисная",
        "product_code": "TBL-001",
        "version": "1.0",
        "lines": [
            {
                "line_number": 1,
                "description": "Панель ДСП",
                "quantity": 2.0,
                "unit": "шт",
            }
        ],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["product_name"] == "Тумба офисная"
    assert data["status"] == "draft"
    assert len(data["lines"]) == 1


@pytest.mark.asyncio
async def test_list_boms(client: AsyncClient, bom):
    resp = await client.get("/api/boms")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    ids = [b["id"] for b in data["items"]]
    assert str(bom.id) in ids


@pytest.mark.asyncio
async def test_list_boms_filter_by_status(client: AsyncClient, bom):
    resp = await client.get("/api/boms", params={"status": "draft"})
    assert resp.status_code == 200
    for b in resp.json()["items"]:
        assert b["status"] == "draft"


@pytest.mark.asyncio
async def test_get_bom(client: AsyncClient, bom_with_lines):
    resp = await client.get(f"/api/boms/{bom_with_lines.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(bom_with_lines.id)
    assert len(data["lines"]) >= 1


@pytest.mark.asyncio
async def test_get_bom_not_found(client: AsyncClient):
    resp = await client.get(f"/api/boms/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_bom(client: AsyncClient, bom):
    resp = await client.patch(f"/api/boms/{bom.id}", json={
        "version": "1.1",
        "notes": "Обновлённая версия",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == "1.1"
    assert data["notes"] == "Обновлённая версия"


# ── BOM Lines ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_bom_line(client: AsyncClient, bom):
    resp = await client.post(f"/api/boms/{bom.id}/lines", json={
        "line_number": 1,
        "description": "Болт М6",
        "quantity": 8.0,
        "unit": "шт",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["description"] == "Болт М6"
    assert data["quantity"] == 8.0
    assert data["bom_id"] == str(bom.id)


@pytest.mark.asyncio
async def test_delete_bom_line(client: AsyncClient, bom_with_lines):
    # Get the line id from the BOM
    bom_resp = await client.get(f"/api/boms/{bom_with_lines.id}")
    line_id = bom_resp.json()["lines"][0]["id"]

    resp = await client.delete(f"/api/boms/{bom_with_lines.id}/lines/{line_id}")
    assert resp.status_code == 200


# ── Approve ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_bom(client: AsyncClient, bom_with_lines):
    resp = await client.post(f"/api/boms/{bom_with_lines.id}/approve")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["approved_by"] is not None


# ── Stock check ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bom_stock_check_no_inventory(client: AsyncClient, bom_with_lines):
    resp = await client.get(f"/api/boms/{bom_with_lines.id}/stock-check", params={"batch_qty": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["bom_id"] == str(bom_with_lines.id)
    assert "lines" in data
    assert "can_produce" in data
    assert "shortage_count" in data


@pytest.mark.asyncio
async def test_bom_stock_check_not_found(client: AsyncClient):
    resp = await client.get(f"/api/boms/{uuid.uuid4()}/stock-check")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_e4_where_used_and_derive_mbom(client: AsyncClient, db_session):
    """E4: rich line fields round-trip, where-used finds usages, an approved
    EBOM derives a draft MBOM copy linked to its source."""
    from app.db.models import CanonicalItem

    item = CanonicalItem(name="Подшипник 6205")
    db_session.add(item)
    await db_session.commit()

    created = await client.post("/api/boms", json={
        "product_name": "Редуктор",
        "version": "1.0",
        "kind": "ebom",
        "lines": [
            {
                "line_number": 1, "description": "Подшипник 6205", "quantity": 2,
                "unit": "шт", "canonical_item_id": str(item.id),
                "position": "5", "reference_designator": "П1",
                "variant": "исп.01",
                "substitutes": [{"description": "Подшипник 6205-2RS", "note": "закрытый"}],
            },
        ],
    })
    assert created.status_code == 201
    ebom = created.json()
    assert ebom["kind"] == "ebom"
    line = ebom["lines"][0]
    assert line["position"] == "5" and line["variant"] == "исп.01"
    assert line["substitutes"][0]["description"] == "Подшипник 6205-2RS"

    used = await client.get(f"/api/boms/where-used?canonical_item_id={item.id}")
    assert used.status_code == 200
    body = used.json()
    assert body["total"] == 1
    assert body["entries"][0]["product_name"] == "Редуктор"

    by_text = await client.get("/api/boms/where-used?query=6205")
    assert by_text.json()["total"] >= 1

    # deriving from a draft EBOM is refused; approve, then derive
    early = await client.post(f"/api/boms/{ebom['id']}/derive-mbom")
    assert early.status_code == 409
    approved = await client.post(f"/api/boms/{ebom['id']}/approve", json={"approved_by": "chief"})
    assert approved.status_code == 200
    derived = await client.post(f"/api/boms/{ebom['id']}/derive-mbom")
    assert derived.status_code == 201
    mbom = derived.json()
    assert mbom["kind"] == "mbom"
    assert mbom["source_bom_id"] == ebom["id"]
    assert mbom["status"] == "draft"
    assert len(mbom["lines"]) == 1
    assert mbom["lines"][0]["position"] == "5"
    # an MBOM cannot be re-derived
    assert (await client.post(f"/api/boms/{mbom['id']}/derive-mbom")).status_code == 409
