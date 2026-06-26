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
async def test_control_plane_status_counts_review_queues(client: AsyncClient):
    await client.post("/api/agent/tasks/propose", json={
        "objective": "Проверить источник",
        "rationale": "Нужна очередь review",
    })
    await client.post("/api/memory/promotions", json={
        "title": "Факт для review queue",
        "summary": "Достаточно длинная формулировка факта для проверки.",
        "metadata": {"url": "https://example.com/fact"},
    })
    await client.post("/api/memory/sources/propose", json={
        "title": "Источник для review queue",
        "url": "https://example.com/source",
    })
    await client.post("/api/technology/learning-rules", json={
        "rule_type": "behavior",
        "entity_type": "agent",
        "field_name": "supplier_catalog_search",
        "replacement_value": "Сначала проверяй официальный каталог поставщика.",
        "confidence": 0.8,
        "occurrences": 2,
    })

    resp = await client.get("/api/agent/control-plane/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks_proposed"] >= 1
    assert data["memory_promotions_pending"] >= 1
    assert data["web_sources_proposed"] >= 1
    assert data["learning_rules_proposed"] >= 1


@pytest.mark.asyncio
async def test_control_plane_status_does_not_count_rejected_tasks_as_open(client: AsyncClient):
    created = await client.post("/api/agent/tasks", json={
        "objective": "Открытая задача",
        "role": "analyst",
    })
    assert created.status_code == 200
    proposed = await client.post("/api/agent/tasks/propose", json={
        "objective": "Отклоняемая задача",
        "rationale": "Проверка счетчика",
    })
    assert proposed.status_code == 200
    rejected = await client.post(
        f"/api/agent/tasks/{proposed.json()['id']}/decide",
        json={"approved": False, "decided_by": "tester"},
    )
    assert rejected.status_code == 200

    resp = await client.get("/api/agent/control-plane/status")

    assert resp.status_code == 200
    assert resp.json()["tasks_open"] == 1


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
async def test_propose_agent_task_requires_later_approval(client: AsyncClient):
    resp = await client.post("/api/agent/tasks/propose", json={
        "objective": "Найти каталоги поставщиков крепежа",
        "description": "Подготовить источники для регулярного мониторинга цен",
        "role": "procurement_specialist",
        "rationale": "Не хватает внешних каталогов для сверки цен",
        "suggested_trigger": "weekly",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "proposed"
    assert data["metadata"]["approval_required"] is True
    assert data["metadata"]["rationale"] == "Не хватает внешних каталогов для сверки цен"


@pytest.mark.asyncio
async def test_decide_agent_task_proposal(client: AsyncClient):
    proposed = await client.post("/api/agent/tasks/propose", json={
        "objective": "Проверить новые каталоги поставщиков",
        "role": "procurement_specialist",
        "rationale": "Нужна фоновая проверка источников",
    })
    assert proposed.status_code == 200
    task_id = proposed.json()["id"]

    decided = await client.post(
        f"/api/agent/tasks/{task_id}/decide",
        json={"approved": True, "decided_by": "tester", "comment": "run it"},
    )
    assert decided.status_code == 200
    data = decided.json()
    assert data["status"] == "created"
    assert data["metadata"]["decision_status"] == "approved"
    assert data["metadata"]["decided_by"] == "tester"


@pytest.mark.asyncio
async def test_agent_service_account_cannot_decide_task(client: AsyncClient):
    """task_decide is the human boundary; the agent service account is rejected."""
    from app.auth.jwt import get_current_user
    from app.auth.models import UserInfo, UserRole
    from app.main import app

    proposed = await client.post("/api/agent/tasks/propose", json={
        "objective": "Сам себя утвердить нельзя",
        "role": "worker",
    })
    assert proposed.status_code == 200
    task_id = proposed.json()["id"]

    agent_user = UserInfo(
        sub="agent-service",
        email="agent@internal",
        name="AI Agent",
        preferred_username="agent",
        roles=[UserRole.admin],
        groups=["agents"],
    )
    app.dependency_overrides[get_current_user] = lambda: agent_user
    try:
        denied = await client.post(
            f"/api/agent/tasks/{task_id}/decide",
            json={"approved": True},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_run_created_agent_task(client: AsyncClient, monkeypatch):
    async def fake_run(prompt: str):
        return True, f"done: {prompt[:30]}"

    monkeypatch.setattr(
        "app.tasks.agent_cron.run_headless_agent_turn",
        fake_run,
    )

    created = await client.post("/api/agent/tasks", json={
        "objective": "Сформировать краткий отчёт",
        "description": "Использовать тестовый headless runner",
        "role": "analyst",
    })
    assert created.status_code == 200

    run = await client.post(f"/api/agent/tasks/{created.json()['id']}/run")

    assert run.status_code == 200
    data = run.json()
    assert data["status"] == "completed"
    assert data["output"].startswith("done:")
    assert data["metadata"]["run_status"] == "completed"


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
