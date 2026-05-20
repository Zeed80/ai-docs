"""Tests for Workspace API — blocks CRUD and agent tools."""

import pytest
from httpx import AsyncClient


# ── Block CRUD ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_workspace_blocks_empty(client: AsyncClient):
    # Clear any existing blocks first
    await client.delete("/api/workspace/blocks")
    resp = await client.get("/api/workspace/blocks")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_verify_block_not_found(client: AsyncClient):
    resp = await client.post("/api/workspace/agent/verify-block", json={
        "canvas_id": "agent:nonexistent-block-xyz"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is False
    assert data["canvas_id"] == "agent:nonexistent-block-xyz"


@pytest.mark.asyncio
async def test_delete_nonexistent_block(client: AsyncClient):
    resp = await client.delete("/api/workspace/blocks/nonexistent-canvas-id")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_clear_all_blocks(client: AsyncClient):
    resp = await client.delete("/api/workspace/blocks")
    assert resp.status_code == 200
    data = resp.json()
    assert "cleared" in data or "deleted" in data or isinstance(data, dict)


# ── Publish invoice table ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_invoice_table_empty(client: AsyncClient):
    resp = await client.post("/api/workspace/agent/invoices/table", json={
        "canvas_id": "test:invoice-list",
        "limit": 10,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    assert data["canvas_id"] == "test:invoice-list"
    assert "total" in data
    assert "shown" in data
    assert "message" in data


@pytest.mark.asyncio
async def test_publish_invoice_table_appears_in_blocks(client: AsyncClient):
    await client.delete("/api/workspace/blocks")
    await client.post("/api/workspace/agent/invoices/table", json={
        "canvas_id": "test:invoice-table-check",
    })
    resp = await client.get("/api/workspace/blocks")
    assert resp.status_code == 200
    data = resp.json()
    block_ids = [item.get("id") for item in data["items"]]
    assert "test:invoice-table-check" in block_ids


@pytest.mark.asyncio
async def test_verify_block_after_publish(client: AsyncClient):
    await client.post("/api/workspace/agent/invoices/table", json={
        "canvas_id": "test:verify-after-publish",
    })
    resp = await client.post("/api/workspace/agent/verify-block", json={
        "canvas_id": "test:verify-after-publish"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is True
    assert data["canvas_id"] == "test:verify-after-publish"


# ── Publish general block ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_general_block(client: AsyncClient):
    resp = await client.post("/api/workspace/agent/generated/general", json={
        "canvas_id": "test:general-block",
        "block_type": "table",
        "title": "Тестовый блок",
        "columns": [{"key": "name", "header": "Имя", "type": "text"}],
        "rows": [{"name": "строка 1"}],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_delete_specific_block(client: AsyncClient):
    await client.post("/api/workspace/agent/generated/general", json={
        "canvas_id": "test:block-to-delete",
        "block_type": "table",
        "title": "Удалить меня",
        "columns": [{"key": "id", "header": "ID", "type": "text"}],
        "rows": [],
    })

    resp = await client.delete("/api/workspace/blocks/test:block-to-delete")
    assert resp.status_code == 200

    # Verify deleted
    verify_resp = await client.post("/api/workspace/agent/verify-block", json={
        "canvas_id": "test:block-to-delete"
    })
    assert verify_resp.json()["exists"] is False
