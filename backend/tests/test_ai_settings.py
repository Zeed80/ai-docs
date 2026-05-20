"""Tests for AI Settings API — config, agent config, models."""

import pytest
from httpx import AsyncClient


# ── Config ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_config(client: AsyncClient):
    resp = await client.get("/api/ai/config")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


@pytest.mark.asyncio
async def test_get_config_status(client: AsyncClient):
    resp = await client.get("/api/ai/config/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "ok" in data
    assert "ollama_available" in data
    assert "warnings" in data
    assert isinstance(data["warnings"], list)


@pytest.mark.asyncio
async def test_update_config(client: AsyncClient):
    resp = await client.patch("/api/ai/config", json={
        "model_agent": "qwen3.5:9b",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


# ── Agent config ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_agent_config(client: AsyncClient):
    resp = await client.get("/api/ai/agent-config")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    assert "model" in data or "max_iterations" in data or "enabled" in data


@pytest.mark.asyncio
async def test_patch_agent_config(client: AsyncClient):
    resp = await client.patch("/api/ai/agent-config", json={
        "max_iterations": 15,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


@pytest.mark.asyncio
async def test_reset_agent_config(client: AsyncClient):
    resp = await client.post("/api/ai/agent-config/reset")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


# ── Models ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_models(client: AsyncClient):
    """List available Ollama models — may be empty if Ollama is not running."""
    resp = await client.get("/api/ai/models")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, (list, dict))


@pytest.mark.asyncio
async def test_models_capabilities(client: AsyncClient):
    resp = await client.get("/api/ai/models/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, (list, dict))


@pytest.mark.asyncio
async def test_models_discover(client: AsyncClient):
    resp = await client.get("/api/ai/models/discover")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, (list, dict))


# ── Agent skills ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_agent_skills(client: AsyncClient):
    resp = await client.get("/api/ai/agent-skills")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, (list, dict))


# ── Embedding profile ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_embedding_profile(client: AsyncClient):
    resp = await client.get("/api/ai/embedding-profile")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
