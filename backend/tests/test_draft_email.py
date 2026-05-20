"""Tests for Draft Email API (/api/draft-emails)."""

import pytest
from httpx import AsyncClient

BASE = "/api/draft-emails"


async def _create(client: AsyncClient, **kwargs) -> dict:
    payload = {
        "to_addresses": ["vendor@example.com"],
        "subject": "Test Draft",
        "body_text": "Hello.",
        **kwargs,
    }
    r = await client.post(BASE, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_create_draft_email(client: AsyncClient):
    data = await _create(
        client,
        subject="Запрос КП",
        body_text="Уважаемые партнёры, прошу предоставить КП.",
        related_entity_type="invoice",
    )
    assert data["subject"] == "Запрос КП"
    assert data["status"] == "draft"
    assert "vendor@example.com" in data["to_addresses"]


@pytest.mark.asyncio
async def test_list_drafts(client: AsyncClient):
    await _create(client, subject="Draft list test")
    r = await client.get(BASE)
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_get_draft_not_found(client: AsyncClient):
    r = await client.get(f"{BASE}/00000000-0000-0000-0000-000000000099")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_draft_by_id(client: AsyncClient):
    draft = await _create(client, subject="Get by ID")
    r = await client.get(f"{BASE}/{draft['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == draft["id"]


@pytest.mark.asyncio
async def test_update_draft(client: AsyncClient):
    draft = await _create(client, subject="Before patch")
    r = await client.patch(
        f"{BASE}/{draft['id']}", json={"subject": "After patch"}
    )
    assert r.status_code == 200
    assert r.json()["subject"] == "After patch"


@pytest.mark.asyncio
async def test_cancel_draft(client: AsyncClient):
    draft = await _create(client, subject="To cancel")
    r = await client.delete(f"{BASE}/{draft['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_nonexistent_draft(client: AsyncClient):
    r = await client.delete(f"{BASE}/00000000-0000-0000-0000-000000000088")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_risk_check_returns_flags(client: AsyncClient):
    draft = await _create(
        client,
        subject="Risk check",
        body_text="Please transfer funds urgently to account 12345.",
    )
    r = await client.post(f"{BASE}/{draft['id']}/risk-check")
    assert r.status_code == 200
    data = r.json()
    assert "risk_flags" in data
    assert isinstance(data["risk_flags"], list)
    assert "risk_score" in data


@pytest.mark.asyncio
async def test_send_draft_creates_approval_or_sends(client: AsyncClient):
    draft = await _create(client, subject="Send test")
    r = await client.post(f"{BASE}/{draft['id']}/send")
    assert r.status_code in (200, 201, 202)
    assert "status" in r.json()


@pytest.mark.asyncio
async def test_list_drafts_pagination(client: AsyncClient):
    for i in range(3):
        await _create(client, subject=f"Draft {i}")
    r = await client.get(f"{BASE}?limit=2&offset=0")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert len(data["items"]) <= 2
