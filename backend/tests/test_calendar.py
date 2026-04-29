"""Tests for Calendar & Reminders API."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.db.models import (
    Document, DocumentStatus, Invoice, InvoiceStatus, Party, PartyRole,
)


@pytest.fixture
async def invoice_with_dates(db_session):
    """Invoice with date metadata for extraction."""
    doc = Document(
        file_name="cal.pdf", file_hash="calh", file_size=100,
        mime_type="application/pdf", storage_path="c/1.pdf",
        status=DocumentStatus.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()

    due = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    inv = Invoice(
        document_id=doc.id, invoice_number="CAL-001", currency="RUB",
        total_amount=5000.0, status=InvoiceStatus.needs_review,
        invoice_date=datetime.now(timezone.utc),
        metadata_={"due_date": due, "delivery_date": due},
    )
    db_session.add(inv)
    await db_session.commit()
    return inv


@pytest.mark.asyncio
async def test_create_event(client: AsyncClient):
    event_date = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    resp = await client.post("/api/calendar/events", json={
        "title": "Оплата счёта",
        "event_date": event_date,
        "event_type": "payment",
        "source": "manual",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Оплата счёта"
    assert data["event_type"] == "payment"


@pytest.mark.asyncio
async def test_list_events(client: AsyncClient):
    event_date = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    await client.post("/api/calendar/events", json={
        "title": "Test list",
        "event_date": event_date,
        "event_type": "due_date",
    })
    resp = await client.get("/api/calendar/events")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_list_events_filter_type(client: AsyncClient):
    event_date = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    await client.post("/api/calendar/events", json={
        "title": "Meeting",
        "event_date": event_date,
        "event_type": "meeting",
    })
    resp = await client.get("/api/calendar/events", params={"event_type": "meeting"})
    assert resp.status_code == 200
    for e in resp.json():
        assert e["event_type"] == "meeting"


@pytest.mark.asyncio
async def test_extract_dates(client: AsyncClient, invoice_with_dates):
    resp = await client.post("/api/calendar/extract-dates", json={
        "invoice_id": str(invoice_with_dates.id),
    })
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["dates"]) >= 2  # invoice_date + due_date + delivery
    assert data["events_created"] >= 2

    # Running again should not create duplicates
    resp2 = await client.post("/api/calendar/extract-dates", json={
        "invoice_id": str(invoice_with_dates.id),
    })
    assert resp2.json()["events_created"] == 0


@pytest.mark.asyncio
async def test_extract_dates_not_found(client: AsyncClient):
    resp = await client.post("/api/calendar/extract-dates", json={
        "invoice_id": str(uuid.uuid4()),
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upcoming(client: AsyncClient):
    event_date = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    await client.post("/api/calendar/events", json={
        "title": "Upcoming test",
        "event_date": event_date,
        "event_type": "due_date",
    })
    resp = await client.get("/api/calendar/upcoming", params={"days": 7})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) >= 1
    assert isinstance(data["reminders"], list)


@pytest.mark.asyncio
async def test_create_and_mark_reminder(client: AsyncClient, invoice_with_dates):
    remind_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    create_resp = await client.post("/api/calendar/reminders", json={
        "entity_type": "invoice",
        "entity_id": str(invoice_with_dates.id),
        "remind_at": remind_at,
        "message": "Не забыть оплатить",
    })
    assert create_resp.status_code == 200
    reminder = create_resp.json()
    assert reminder["is_sent"] is False

    # Mark as sent
    resp = await client.post(f"/api/calendar/reminders/{reminder['id']}/mark-sent")
    assert resp.status_code == 200
    assert resp.json()["is_sent"] is True
    assert resp.json()["sent_at"] is not None

    # Double mark fails
    resp2 = await client.post(f"/api/calendar/reminders/{reminder['id']}/mark-sent")
    assert resp2.status_code == 400


@pytest.mark.asyncio
async def test_list_reminders(client: AsyncClient, invoice_with_dates):
    remind_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    await client.post("/api/calendar/reminders", json={
        "entity_type": "invoice",
        "entity_id": str(invoice_with_dates.id),
        "remind_at": remind_at,
        "message": "List test reminder",
    })
    resp = await client.get("/api/calendar/reminders", params={"is_sent": False})
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
