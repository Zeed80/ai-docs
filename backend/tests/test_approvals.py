"""Approval API tests — approval.request, approval.status, approval.list_pending"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_approval(client: AsyncClient):
    """approval.request — create a pending approval."""
    resp = await client.post(
        "/api/approvals",
        json={
            "action_type": "invoice.approve",
            "entity_type": "invoice",
            "entity_id": "00000000-0000-0000-0000-000000000001",
            "requested_by": "sveta",
            "context": {"invoice_number": "INV-001", "total": 15000},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["action_type"] == "invoice.approve"
    assert data["requested_by"] == "sveta"


@pytest.mark.asyncio
async def test_get_approval(client: AsyncClient):
    """approval.status — get approval by ID."""
    create = await client.post(
        "/api/approvals",
        json={
            "action_type": "email.send",
            "entity_type": "email_draft",
            "entity_id": "00000000-0000-0000-0000-000000000002",
        },
    )
    approval_id = create.json()["id"]

    resp = await client.get(f"/api/approvals/{approval_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == approval_id


@pytest.mark.asyncio
async def test_list_pending(client: AsyncClient):
    """approval.list_pending — returns pending approvals."""
    await client.post(
        "/api/approvals",
        json={
            "action_type": "invoice.approve",
            "entity_type": "invoice",
            "entity_id": "00000000-0000-0000-0000-000000000003",
        },
    )

    resp = await client.get("/api/approvals/pending")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_decide_approval(client: AsyncClient):
    """Decide on approval — approve it."""
    create = await client.post(
        "/api/approvals",
        json={
            "action_type": "invoice.approve",
            "entity_type": "invoice",
            "entity_id": "00000000-0000-0000-0000-000000000004",
        },
    )
    approval_id = create.json()["id"]

    resp = await client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"status": "approved", "comment": "Looks good", "decided_by": "user1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["decided_by"] == "user1"


@pytest.mark.asyncio
async def test_decide_already_decided(client: AsyncClient):
    """Cannot decide on already decided approval."""
    create = await client.post(
        "/api/approvals",
        json={
            "action_type": "invoice.approve",
            "entity_type": "invoice",
            "entity_id": "00000000-0000-0000-0000-000000000005",
        },
    )
    approval_id = create.json()["id"]

    await client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"status": "approved"},
    )

    resp = await client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"status": "rejected"},
    )
    assert resp.status_code == 400
