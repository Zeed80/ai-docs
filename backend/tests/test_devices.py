"""Tests for the device registry API (/api/devices) used by mobile push."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_register_creates_device_with_topic(client):
    resp = await client.post(
        "/api/devices/register",
        json={"platform": "android", "app_version": "0.1.0"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ntfy_topic"].startswith("sveta-")
    assert data["platform"] == "android"
    assert data["enabled"] is True


@pytest.mark.asyncio
async def test_register_is_idempotent_per_topic(client):
    first = (await client.post("/api/devices/register", json={})).json()
    again = await client.post(
        "/api/devices/register", json={"ntfy_topic": first["ntfy_topic"]}
    )
    assert again.status_code == 200
    # same topic → same device row
    assert again.json()["id"] == first["id"]


@pytest.mark.asyncio
async def test_list_and_delete_device(client):
    created = (await client.post("/api/devices/register", json={})).json()

    listed = await client.get("/api/devices")
    assert listed.status_code == 200
    assert any(d["id"] == created["id"] for d in listed.json())

    deleted = await client.delete(f"/api/devices/{created['id']}")
    assert deleted.status_code == 204

    after = await client.get("/api/devices")
    assert all(d["id"] != created["id"] for d in after.json())
