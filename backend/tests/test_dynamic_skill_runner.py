"""Tests for Dynamic Skill Runner API — generated skills and evolution."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_generated_skills(client: AsyncClient):
    resp = await client.get("/api/agent/generated-skills")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


@pytest.mark.asyncio
async def test_skill_evolution_audit(client: AsyncClient):
    resp = await client.get("/api/agent/skill-evolution/audit")
    assert resp.status_code == 200
    data = resp.json()
    assert "entries" in data
    assert "count" in data
    assert isinstance(data["entries"], list)


@pytest.mark.asyncio
async def test_skill_cache_stats(client: AsyncClient):
    resp = await client.get("/api/agent/skill-cache/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
