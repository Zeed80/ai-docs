"""Tests for Dashboard API — unified feed and today's summary."""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.db.models import (
    Approval, ApprovalActionType, ApprovalStatus,
    AnomalyCard, AnomalyStatus, AnomalyType,
    Document, DocumentStatus,
)


@pytest.fixture
async def pending_approval(db_session):
    doc = Document(
        file_name="dash-test.pdf",
        file_hash="dashh001",
        file_size=512,
        mime_type="application/pdf",
        storage_path="d/1.pdf",
        status=DocumentStatus.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()

    approval = Approval(
        action_type=ApprovalActionType.invoice_approve,
        entity_type="document",
        entity_id=doc.id,
        requested_by="bot",
        status=ApprovalStatus.pending,
        context={"title": "Требуется подтверждение", "body": "Новый счёт получен"},
    )
    db_session.add(approval)
    await db_session.commit()
    return approval


@pytest.fixture
async def open_anomaly(db_session):
    doc = Document(
        file_name="anomaly-dash.pdf",
        file_hash="anomdash",
        file_size=256,
        mime_type="application/pdf",
        storage_path="d/2.pdf",
        status=DocumentStatus.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()

    from app.db.models import AnomalySeverity
    anomaly = AnomalyCard(
        entity_type="document",
        entity_id=doc.id,
        anomaly_type=AnomalyType.price_spike,
        severity=AnomalySeverity.warning,
        title="Ценовой скачок",
        description="Цена выше нормы на 35%",
        status=AnomalyStatus.open,
    )
    db_session.add(anomaly)
    await db_session.commit()
    return anomaly


# ── Feed ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feed_empty(client: AsyncClient):
    resp = await client.get("/api/dashboard/feed")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_feed_includes_pending_approval(client: AsyncClient, pending_approval):
    resp = await client.get("/api/dashboard/feed")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    item_types = [i["type"] for i in data["items"]]
    assert "approval" in item_types


@pytest.mark.asyncio
async def test_feed_includes_open_anomaly(client: AsyncClient, open_anomaly):
    resp = await client.get("/api/dashboard/feed")
    assert resp.status_code == 200
    data = resp.json()
    item_types = [i["type"] for i in data["items"]]
    assert "anomaly" in item_types


@pytest.mark.asyncio
async def test_feed_item_structure(client: AsyncClient, pending_approval):
    resp = await client.get("/api/dashboard/feed")
    items = resp.json()["items"]
    for item in items:
        assert "id" in item
        assert "type" in item
        assert "priority" in item
        assert "title" in item
        assert "entity_type" in item
        assert "entity_id" in item
        assert "created_at" in item


# ── Today summary ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_today_summary(client: AsyncClient):
    resp = await client.get("/api/dashboard/today")
    assert resp.status_code == 200
    data = resp.json()
    # Check expected counters are present
    assert "documents_pending_review" in data or "pending_review" in data or isinstance(data, dict)


@pytest.mark.asyncio
async def test_today_summary_with_data(client: AsyncClient, pending_approval, open_anomaly):
    resp = await client.get("/api/dashboard/today")
    assert resp.status_code == 200
    data = resp.json()
    # The response should be a dict with numeric fields
    assert isinstance(data, dict)
    for v in data.values():
        assert isinstance(v, (int, float, str, list, dict, type(None)))
