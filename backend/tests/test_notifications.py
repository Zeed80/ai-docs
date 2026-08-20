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


# ── Feedback (accept/dismiss/snooze) — AGENT_AUTONOMY_ROADMAP.md Ф0.B ─────────


@pytest.fixture
async def proactive_notification(db_session):
    """A notification from a per-user proactive task (source_task set) —
    the kind feedback can be calibrated against."""
    n = Notification(
        user_sub="dev-user",
        type=NotificationType.document_ready,
        title="Приближается срок оплаты",
        body="Счёт №1 — срок оплаты 25.05.2026",
        entity_type="invoice",
        entity_id=uuid.uuid4(),
        source_task="proactive.check_due_dates",
        is_read=False,
    )
    db_session.add(n)
    await db_session.commit()
    return n


@pytest.mark.asyncio
async def test_feedback_not_found(client: AsyncClient):
    resp = await client.post(
        f"/api/notifications/{uuid.uuid4()}/feedback", json={"action": "accepted"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_feedback_without_source_task_marks_read_but_not_calibrated(
    client: AsyncClient, notification
):
    """`notification` fixture has no source_task (mimics approval/mention/system)."""
    resp = await client.post(
        f"/api/notifications/{notification.id}/feedback", json={"action": "dismissed"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["calibrated"] is False

    listing = await client.get("/api/notifications", params={"unread": False})
    ids = [n["id"] for n in listing.json()["items"]]
    assert str(notification.id) in ids


@pytest.mark.asyncio
async def test_feedback_accepted_is_calibrated_and_recorded(
    client: AsyncClient, proactive_notification, db_session
):
    from app.db.models import ProactiveTaskFeedback
    from sqlalchemy import select

    resp = await client.post(
        f"/api/notifications/{proactive_notification.id}/feedback",
        json={"action": "accepted"},
    )
    assert resp.status_code == 200
    assert resp.json()["calibrated"] is True

    row = (
        await db_session.execute(
            select(ProactiveTaskFeedback).where(
                ProactiveTaskFeedback.notification_id == proactive_notification.id
            )
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.beat_task_name == "proactive.check_due_dates"
    assert row.action == "accepted"
    assert row.user_sub == "dev-user"
    assert row.entity_type == "invoice"


@pytest.mark.asyncio
async def test_feedback_snoozed_sets_snoozed_until(
    client: AsyncClient, proactive_notification, db_session
):
    from app.db.models import ProactiveTaskFeedback
    from sqlalchemy import select

    resp = await client.post(
        f"/api/notifications/{proactive_notification.id}/feedback",
        json={"action": "snoozed", "snooze_minutes": 120},
    )
    assert resp.status_code == 200

    row = (
        await db_session.execute(
            select(ProactiveTaskFeedback).where(
                ProactiveTaskFeedback.notification_id == proactive_notification.id
            )
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.action == "snoozed"
    assert row.snoozed_until is not None


@pytest.mark.asyncio
async def test_feedback_calibrates_acceptance_rate(
    client: AsyncClient, db_session
):
    """End-to-end: several accept/dismiss reactions move the acceptance rate."""
    from app.domain.proactive_feedback import get_proactive_task_acceptance_rate

    for i, action in enumerate(["dismissed"] * 8 + ["accepted"] * 2):
        n = Notification(
            user_sub="dev-user",
            type=NotificationType.document_ready,
            title=f"Напоминание {i}",
            body="test",
            source_task="proactive.dispatch_due_reminders",
            is_read=False,
        )
        db_session.add(n)
        await db_session.commit()
        resp = await client.post(
            f"/api/notifications/{n.id}/feedback", json={"action": action}
        )
        assert resp.status_code == 200

    rate = await get_proactive_task_acceptance_rate(
        db_session, "proactive.dispatch_due_reminders", min_sample_size=5
    )
    assert rate.status == "ok"
    assert rate.rate == pytest.approx(0.2)
