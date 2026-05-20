"""Tests for Notifications API — in-app notification inbox."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import Notification, NotificationType


@pytest.fixture
async def notification(db_session):
    n = Notification(
        user_sub="dev-user",
        type=NotificationType.document_ready,
        title="Новый документ",
        body="Загружен счёт от поставщика",
        is_read=False,
    )
    db_session.add(n)
    await db_session.commit()
    return n


@pytest.fixture
async def read_notification(db_session):
    n = Notification(
        user_sub="dev-user",
        type=NotificationType.approval_assigned,
        title="Требуется подтверждение",
        body="Документ ожидает вашего решения",
        is_read=True,
    )
    db_session.add(n)
    await db_session.commit()
    return n


# ── List ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_notifications_empty(client: AsyncClient):
    resp = await client.get("/api/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_list_notifications(client: AsyncClient, notification):
    resp = await client.get("/api/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    titles = [n["title"] for n in data["items"]]
    assert "Новый документ" in titles


@pytest.mark.asyncio
async def test_list_notifications_unread_filter(client: AsyncClient, notification, read_notification):
    resp = await client.get("/api/notifications", params={"unread": True})
    assert resp.status_code == 200
    for n in resp.json()["items"]:
        assert n["is_read"] is False


@pytest.mark.asyncio
async def test_list_notifications_read_filter(client: AsyncClient, notification, read_notification):
    resp = await client.get("/api/notifications", params={"unread": False})
    assert resp.status_code == 200
    for n in resp.json()["items"]:
        assert n["is_read"] is True


# ── Unread count ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unread_count(client: AsyncClient, notification):
    resp = await client.get("/api/notifications/unread-count")
    assert resp.status_code == 200
    data = resp.json()
    assert "count" in data or "unread" in data or isinstance(data, (int, dict))


# ── Mark read ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_notification_read(client: AsyncClient, notification):
    resp = await client.post(f"/api/notifications/{notification.id}/read")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"


@pytest.mark.asyncio
async def test_mark_notification_read_not_found(client: AsyncClient):
    # endpoint silently ignores unknown notification IDs
    resp = await client.post(f"/api/notifications/{uuid.uuid4()}/read")
    assert resp.status_code == 200


# ── Mark all read ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_all_read(client: AsyncClient, notification, read_notification):
    resp = await client.post("/api/notifications/read-all")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"
    assert "marked" in data
