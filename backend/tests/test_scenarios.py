"""Tests for Scenarios API — list and trigger AiAgent scenarios."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_scenarios(client: AsyncClient):
    resp = await client.get("/api/scenarios")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_run_scenario_not_found(client: AsyncClient):
    resp = await client.post("/api/scenarios/nonexistent-scenario-xyz/run", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reload_agent_config(client: AsyncClient):
    resp = await client.post("/api/scenarios/agent/reload-config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "reloaded"
    assert "exposed_skills" in data
    assert "approval_gates" in data
