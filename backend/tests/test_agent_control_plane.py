"""Tests for Agent Control Plane API — status, tasks, teams, cron, plugins."""

import pytest
from httpx import AsyncClient

from app.db.models import AgentCron, AgentTask, AgentTeam


@pytest.fixture
async def agent_task(db_session):
    task = AgentTask(
        objective="Проверить счета за неделю",
        description="Автоматическая проверка",
        role="analyst",
        status="created",
    )
    db_session.add(task)
    await db_session.commit()
    return task


@pytest.fixture
async def agent_team(db_session):
    team = AgentTeam(
        name="Закупки",
        purpose="Обработка входящих счетов и КП",
        status="created",
    )
    db_session.add(team)
    await db_session.commit()
    return team


@pytest.fixture
async def agent_cron(db_session):
    cron = AgentCron(
        schedule="0 9 * * 1-5",
        prompt="Проверь новые счета и оповести об аномалиях",
        description="Ежедневная утренняя проверка",
        enabled=True,
    )
    db_session.add(cron)
    await db_session.commit()
    return cron


# ── Status ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_control_plane_status(client: AsyncClient):
    resp = await client.get("/api/agent/control-plane/status")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


@pytest.mark.asyncio
async def test_runtime_status(client: AsyncClient):
    resp = await client.get("/api/agent/runtime/status")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


# ── Tasks ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_agent_task(client: AsyncClient):
    resp = await client.post("/api/agent/tasks", json={
        "objective": "Сформировать отчёт по закупкам",
        "role": "analyst",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["objective"] == "Сформировать отчёт по закупкам"


@pytest.mark.asyncio
async def test_list_agent_tasks(client: AsyncClient, agent_task):
    resp = await client.get("/api/agent/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    objectives = [t["objective"] for t in data]
    assert "Проверить счета за неделю" in objectives


# ── Teams ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_agent_team(client: AsyncClient):
    resp = await client.post("/api/agent/teams", json={
        "name": "Финансовый отдел",
        "purpose": "Контроль платежей и договоров",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["name"] == "Финансовый отдел"


@pytest.mark.asyncio
async def test_list_agent_teams(client: AsyncClient, agent_team):
    resp = await client.get("/api/agent/teams")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    names = [t["name"] for t in data]
    assert "Закупки" in names


# ── Cron ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_agent_cron(client: AsyncClient):
    resp = await client.post("/api/agent/cron", json={
        "schedule": "0 18 * * 5",
        "prompt": "Сформируй сводку по аномалиям за неделю",
        "description": "Еженедельный пятничный отчёт",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["schedule"] == "0 18 * * 5"


@pytest.mark.asyncio
async def test_list_agent_cron(client: AsyncClient, agent_cron):
    resp = await client.get("/api/agent/cron")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    schedules = [c["schedule"] for c in data]
    assert "0 9 * * 1-5" in schedules


@pytest.mark.asyncio
async def test_patch_agent_cron(client: AsyncClient, agent_cron):
    resp = await client.patch(f"/api/agent/cron/{agent_cron.id}", json={
        "enabled": False,
        "description": "Временно отключено",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False


# ── Plugins ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_agent_plugin(client: AsyncClient):
    resp = await client.post("/api/agent/plugins", json={
        "plugin_key": "test-plugin-001",
        "name": "Тестовый плагин",
        "version": "0.1.0",
        "description": "Плагин для тестирования",
        "manifest": {"tools": [], "permissions": []},
        "risk_level": "low",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["plugin_key"] == "test-plugin-001"
    assert data["enabled"] is False


@pytest.mark.asyncio
async def test_list_agent_plugins(client: AsyncClient):
    resp = await client.get("/api/agent/plugins")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


# ── Skills ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_agent_skills(client: AsyncClient):
    resp = await client.get("/api/agent/skills")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, (list, dict))


# ── Config proposals ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_config_proposal(client: AsyncClient):
    resp = await client.post("/api/agent/config/proposals", json={
        "setting_path": "model",
        "proposed_value": "qwen3.5:14b",
        "reason": "Тест производительности на новой модели",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data


@pytest.mark.asyncio
async def test_list_config_proposals(client: AsyncClient):
    resp = await client.get("/api/agent/config/proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
