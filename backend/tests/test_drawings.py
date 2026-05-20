"""Tests for Drawings API — CRUD, features, review."""

import io
import uuid

import pytest
from httpx import AsyncClient

from app.db.models import Drawing, DrawingStatus, DrawingFeature, DrawingFeatureType


@pytest.fixture
async def drawing(db_session):
    d = Drawing(
        filename="detail-shaft.dxf",
        format="dxf",
        drawing_number="ЧТЖ-001",
        status=DrawingStatus.analyzed,
    )
    db_session.add(d)
    await db_session.commit()
    return d


@pytest.fixture
async def drawing_with_feature(db_session):
    d = Drawing(
        filename="bracket.dxf",
        format="dxf",
        drawing_number="ЧТЖ-002",
        status=DrawingStatus.analyzed,
    )
    db_session.add(d)
    await db_session.flush()

    feat = DrawingFeature(
        drawing_id=d.id,
        feature_type=DrawingFeatureType.hole,
        name="Отверстие Ø12",
        description="Сквозное отверстие под болт М12",
        confidence=0.92,
    )
    db_session.add(feat)
    await db_session.commit()
    return d


# ── List ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_drawings_empty(client: AsyncClient):
    resp = await client.get("/api/drawings")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_list_drawings_includes_entry(client: AsyncClient, drawing):
    resp = await client.get("/api/drawings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    ids = [d["id"] for d in data["items"]]
    assert str(drawing.id) in ids


@pytest.mark.asyncio
async def test_list_drawings_filter_by_status(client: AsyncClient, drawing):
    resp = await client.get("/api/drawings", params={"status": "analyzed"})
    assert resp.status_code == 200
    for d in resp.json()["items"]:
        assert d["status"] == "analyzed"


# ── Get ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_drawing(client: AsyncClient, drawing_with_feature):
    resp = await client.get(f"/api/drawings/{drawing_with_feature.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(drawing_with_feature.id)
    assert data["drawing_number"] == "ЧТЖ-002"
    assert len(data["features"]) >= 1


@pytest.mark.asyncio
async def test_get_drawing_not_found(client: AsyncClient):
    resp = await client.get(f"/api/drawings/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Update ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_drawing(client: AsyncClient, drawing):
    resp = await client.patch(f"/api/drawings/{drawing.id}", json={
        "drawing_number": "ЧТЖ-001-Р",
        "revision": "А",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["drawing_number"] == "ЧТЖ-001-Р"
    assert data["revision"] == "А"


# ── Features ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_feature(client: AsyncClient, drawing):
    resp = await client.post(f"/api/drawings/{drawing.id}/features", json={
        "feature_type": "hole",
        "name": "Центральное отверстие Ø20",
        "description": "Посадочное место вала",
        "confidence": 0.95,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["feature_type"] == "hole"
    assert data["name"] == "Центральное отверстие Ø20"
    assert data["drawing_id"] == str(drawing.id)


@pytest.mark.asyncio
async def test_get_feature(client: AsyncClient, drawing_with_feature):
    drawing_resp = await client.get(f"/api/drawings/{drawing_with_feature.id}")
    feature_id = drawing_resp.json()["features"][0]["id"]

    resp = await client.get(f"/api/drawings/{drawing_with_feature.id}/features/{feature_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == feature_id
    assert data["feature_type"] == "hole"


@pytest.mark.asyncio
async def test_update_feature(client: AsyncClient, drawing_with_feature):
    drawing_resp = await client.get(f"/api/drawings/{drawing_with_feature.id}")
    feature_id = drawing_resp.json()["features"][0]["id"]

    resp = await client.patch(
        f"/api/drawings/{drawing_with_feature.id}/features/{feature_id}",
        json={"name": "Отверстие Ø12 (обновлено)", "confidence": 0.98},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Отверстие Ø12 (обновлено)"


@pytest.mark.asyncio
async def test_review_feature(client: AsyncClient, drawing_with_feature):
    drawing_resp = await client.get(f"/api/drawings/{drawing_with_feature.id}")
    feature_id = drawing_resp.json()["features"][0]["id"]

    resp = await client.post(
        f"/api/drawings/{drawing_with_feature.id}/features/{feature_id}/review",
        json={"action": "approve", "reviewed_by": "engineer"},
    )
    assert resp.status_code in (200, 201)
    data = resp.json()
    assert data.get("reviewed_by") == "engineer" or data.get("status") is not None


@pytest.mark.asyncio
async def test_delete_feature(client: AsyncClient, drawing_with_feature):
    drawing_resp = await client.get(f"/api/drawings/{drawing_with_feature.id}")
    feature_id = drawing_resp.json()["features"][0]["id"]

    resp = await client.delete(
        f"/api/drawings/{drawing_with_feature.id}/features/{feature_id}"
    )
    assert resp.status_code in (200, 204)


# ── Delete drawing ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_delete_drawings(client: AsyncClient, drawing):
    import json as _json
    resp = await client.request(
        "DELETE",
        "/api/drawings/bulk-delete",
        content=_json.dumps({"drawing_ids": [str(drawing.id)]}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "deleted" in data
    assert data["deleted"] == 1
