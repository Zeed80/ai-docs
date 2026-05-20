"""Tests for Handovers API — document routing between users."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import Document, DocumentStatus, Handover


@pytest.fixture
async def source_doc(db_session):
    doc = Document(
        file_name="handover-test.pdf",
        file_hash="hov001",
        file_size=1024,
        mime_type="application/pdf",
        storage_path="h/1.pdf",
        status=DocumentStatus.approved,
    )
    db_session.add(doc)
    await db_session.commit()
    return doc


@pytest.fixture
async def pending_handover(db_session, source_doc):
    # from_user=dev-user matches _DEV_USER.sub for outbox test
    h = Handover(
        entity_type="document",
        entity_id=source_doc.id,
        from_user="dev-user",
        to_user="engineer",
        comment="На проверку",
        status="pending",
    )
    db_session.add(h)
    await db_session.commit()
    return h


# ── Create ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_handover(client: AsyncClient, source_doc):
    resp = await client.post("/api/handovers", json={
        "entity_type": "document",
        "entity_id": str(source_doc.id),
        "to_user": "manager",
        "comment": "Требует проверки руководителем",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert data["to_user"] == "manager"
    assert data["status"] == "pending"


# ── Inbox / Outbox ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_inbox(client: AsyncClient, pending_handover):
    resp = await client.get("/api/handovers/inbox")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_outbox(client: AsyncClient, pending_handover):
    resp = await client.get("/api/handovers/outbox")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    ids = [h["id"] for h in data]
    assert str(pending_handover.id) in ids


# ── Accept ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_handover(client: AsyncClient, source_doc):
    create_resp = await client.post("/api/handovers", json={
        "entity_type": "document",
        "entity_id": str(source_doc.id),
        "to_user": "dev-user",
    })
    handover_id = create_resp.json()["id"]

    resp = await client.post(f"/api/handovers/{handover_id}/accept")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"


@pytest.mark.asyncio
async def test_accept_handover_not_found(client: AsyncClient):
    resp = await client.post(f"/api/handovers/{uuid.uuid4()}/accept")
    assert resp.status_code == 404


# ── Forward ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forward_handover(client: AsyncClient, source_doc):
    create_resp = await client.post("/api/handovers", json={
        "entity_type": "document",
        "entity_id": str(source_doc.id),
        "to_user": "dev-user",
    })
    handover_id = create_resp.json()["id"]

    resp = await client.post(f"/api/handovers/{handover_id}/forward", json={
        "entity_type": "document",
        "entity_id": str(source_doc.id),
        "to_user": "accountant",
        "comment": "Переадресовано в бухгалтерию",
    })
    assert resp.status_code == 200
    data = resp.json()
    # forward returns the new handover (pending), original becomes "forwarded"
    assert data["to_user"] == "accountant"


# ── Return ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_return_handover(client: AsyncClient, source_doc):
    create_resp = await client.post("/api/handovers", json={
        "entity_type": "document",
        "entity_id": str(source_doc.id),
        "to_user": "dev-user",
    })
    handover_id = create_resp.json()["id"]

    resp = await client.post(f"/api/handovers/{handover_id}/return")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "returned"
