"""Tests for Agent Actions API — audit trail for agent steps."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import AgentAction


@pytest.fixture
async def agent_action(db_session):
    action = AgentAction(
        session_id="test-session-001",
        iteration=1,
        action_type="tool_call",
        tool_name="invoices.list",
        tool_args={"limit": 10},
        tool_result={"count": 3},
        model_name="qwen3.5:9b",
        duration_ms=450,
    )
    db_session.add(action)
    await db_session.commit()
    return action


# ── Create ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_agent_action(client: AsyncClient):
    resp = await client.post("/api/agent-actions", json={
        "session_id": "sess-abc",
        "iteration": 1,
        "action_type": "tool_call",
        "tool_name": "documents.list",
        "duration_ms": 200,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["session_id"] == "sess-abc"
    assert data["tool_name"] == "documents.list"
    assert "id" in data


# ── List ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_agent_actions_empty(client: AsyncClient):
    resp = await client.get("/api/agent-actions")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_list_agent_actions(client: AsyncClient, agent_action):
    resp = await client.get("/api/agent-actions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    session_ids = [a["session_id"] for a in data["items"]]
    assert "test-session-001" in session_ids


@pytest.mark.asyncio
async def test_list_agent_actions_filter_by_session(client: AsyncClient, agent_action):
    resp = await client.get("/api/agent-actions", params={"session_id": "test-session-001"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    for a in data["items"]:
        assert a["session_id"] == "test-session-001"


# ── Get ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_agent_action(client: AsyncClient, agent_action):
    resp = await client.get(f"/api/agent-actions/{agent_action.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(agent_action.id)
    assert data["action_type"] == "tool_call"
    assert data["tool_name"] == "invoices.list"


@pytest.mark.asyncio
async def test_get_agent_action_not_found(client: AsyncClient):
    resp = await client.get(f"/api/agent-actions/{uuid.uuid4()}")
    assert resp.status_code == 404
