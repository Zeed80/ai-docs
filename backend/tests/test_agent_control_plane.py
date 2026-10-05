"""Tests for Agent Control Plane API — status, tasks, teams, cron, plugins."""

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.agent_runtime_models import DelegationGrant, DurableChatRun
from app.db.models import (
    AgentCron,
    AgentTask,
    AgentTeam,
    ChatMessage,
    ChatSession,
    WorkOrder,
)
from app.db.work_budget_models import WorkBudgetLedger


@asynccontextmanager
async def _committed_api(test_engine, monkeypatch):
    """Use one real transaction/session per request, including concurrent POSTs."""
    from app.auth.jwt import get_current_user
    from app.auth.models import UserInfo, UserRole
    from app.db.session import get_db
    from app.main import app

    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    async def override_current_user(request: Request):
        owner = request.headers.get("x-test-owner", "dev-user")
        return UserInfo(
            sub=owner,
            email=f"{owner}@example.test",
            name=owner,
            preferred_username=owner,
            roles=[UserRole.admin],
        )

    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as api_client:
            yield api_client, factory
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


async def _delete_committed_agent_task(factory, task_id: uuid.UUID) -> None:
    """Remove only the exact committed graph created by a concurrency test."""
    async with factory() as db:
        orders = list(
            (
                await db.scalars(select(WorkOrder).where(WorkOrder.legacy_agent_task_id == task_id))
            ).all()
        )
        session_ids: set[uuid.UUID] = set()
        for order in orders:
            runs = list(
                (
                    await db.scalars(
                        select(DurableChatRun).where(DurableChatRun.work_order_id == order.id)
                    )
                ).all()
            )
            session_ids.update(run.session_id for run in runs)
            await db.execute(delete(DurableChatRun).where(DurableChatRun.work_order_id == order.id))
            ledger_id = order.budget_ledger_id
            order.budget_ledger_id = None
            await db.flush()
            if ledger_id is not None:
                ledger = await db.get(WorkBudgetLedger, ledger_id)
                if ledger is not None:
                    await db.delete(ledger)
            await db.delete(order)
        task = await db.get(AgentTask, task_id)
        if task is not None:
            await db.delete(task)
        await db.flush()
        if session_ids:
            await db.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids)))
            await db.execute(delete(ChatSession).where(ChatSession.id.in_(session_ids)))
        await db.commit()


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
    await client.post(
        "/api/agent/tasks/propose",
        json={
            "objective": "Проверить источник",
            "rationale": "Нужна очередь review",
        },
    )
    await client.post(
        "/api/memory/promotions",
        json={
            "title": "Факт для review queue",
            "summary": "Достаточно длинная формулировка факта для проверки.",
            "metadata": {"url": "https://example.com/fact"},
        },
    )
    await client.post(
        "/api/memory/sources/propose",
        json={
            "title": "Источник для review queue",
            "url": "https://example.com/source",
        },
    )
    await client.post(
        "/api/technology/learning-rules",
        json={
            "rule_type": "behavior",
            "entity_type": "agent",
            "field_name": "supplier_catalog_search",
            "replacement_value": "Сначала проверяй официальный каталог поставщика.",
            "confidence": 0.8,
            "occurrences": 2,
        },
    )

    resp = await client.get("/api/agent/control-plane/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks_proposed"] >= 1
    assert data["memory_promotions_pending"] >= 1
    assert data["web_sources_proposed"] >= 1
    assert data["learning_rules_proposed"] >= 1


@pytest.mark.asyncio
async def test_control_plane_status_does_not_count_rejected_tasks_as_open(client: AsyncClient):
    created = await client.post(
        "/api/agent/tasks",
        json={
            "objective": "Открытая задача",
            "role": "analyst",
        },
    )
    assert created.status_code == 200
    proposed = await client.post(
        "/api/agent/tasks/propose",
        json={
            "objective": "Отклоняемая задача",
            "rationale": "Проверка счетчика",
        },
    )
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
    resp = await client.post(
        "/api/agent/tasks",
        json={
            "objective": "Сформировать отчёт по закупкам",
            "role": "analyst",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["objective"] == "Сформировать отчёт по закупкам"


@pytest.mark.asyncio
async def test_propose_agent_task_requires_later_approval(client: AsyncClient):
    resp = await client.post(
        "/api/agent/tasks/propose",
        json={
            "objective": "Найти каталоги поставщиков крепежа",
            "description": "Подготовить источники для регулярного мониторинга цен",
            "role": "procurement_specialist",
            "rationale": "Не хватает внешних каталогов для сверки цен",
            "suggested_trigger": "weekly",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "proposed"
    assert data["metadata"]["approval_required"] is True
    assert data["metadata"]["rationale"] == "Не хватает внешних каталогов для сверки цен"


@pytest.mark.asyncio
async def test_decide_agent_task_proposal(client: AsyncClient):
    proposed = await client.post(
        "/api/agent/tasks/propose",
        json={
            "objective": "Проверить новые каталоги поставщиков",
            "role": "procurement_specialist",
            "rationale": "Нужна фоновая проверка источников",
        },
    )
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

    proposed = await client.post(
        "/api/agent/tasks/propose",
        json={
            "objective": "Сам себя утвердить нельзя",
            "role": "worker",
        },
    )
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
async def test_run_created_agent_task_uses_one_durable_intake(
    client: AsyncClient, db_session, monkeypatch
):
    execute = AsyncMock(return_value=False)
    monkeypatch.setattr("app.tasks.work_orders.execute_work_order_now", execute)

    created = await client.post(
        "/api/agent/tasks",
        json={
            "objective": "Сформировать краткий отчёт",
            "description": "Использовать тестовый headless runner",
            "role": "analyst",
        },
    )
    assert created.status_code == 200

    run = await client.post(f"/api/agent/tasks/{created.json()['id']}/run")

    assert run.status_code == 200
    data = run.json()
    assert data["status"] == "running"
    assert data["metadata"]["run_status"] == "ready"
    assert data["metadata"]["work_order_status"] == "ready"
    task_id = uuid.UUID(created.json()["id"])
    order_id = uuid.UUID(data["metadata"]["work_order_id"])
    run_id = uuid.UUID(data["metadata"]["durable_chat_run_id"])
    order = await db_session.get(WorkOrder, order_id)
    durable = await db_session.get(DurableChatRun, run_id)
    ledger = await db_session.get(WorkBudgetLedger, order.budget_ledger_id)
    assert order.source == "durable_chat"
    assert order.legacy_agent_task_id == task_id
    assert order.owner_key == "dev-user"
    assert durable.work_order_id == order.id
    assert durable.external_message_id == f"agent-task:{task_id}"
    assert ledger.root_work_order_id == order.id
    assert ledger.owner_key == "dev-user"
    assert execute.await_count == 1

    repeated = await client.post(f"/api/agent/tasks/{created.json()['id']}/run")
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "running"
    assert repeated.json()["metadata"]["work_order_id"] == str(order_id)
    assert execute.await_count == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(WorkOrder)
            .where(WorkOrder.legacy_agent_task_id == task_id)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_agent_task_retry_rejects_changed_input_without_new_executor(
    client: AsyncClient, db_session, monkeypatch
):
    execute = AsyncMock(return_value=False)
    monkeypatch.setattr("app.tasks.work_orders.execute_work_order_now", execute)
    created = await client.post(
        "/api/agent/tasks",
        json={"objective": "Исходная цель", "description": "Исходный контекст"},
    )
    task_id = uuid.UUID(created.json()["id"])
    first = await client.post(f"/api/agent/tasks/{task_id}/run")
    assert first.status_code == 200
    order_id = uuid.UUID(first.json()["metadata"]["work_order_id"])
    order = await db_session.get(WorkOrder, order_id)
    ledger_id = order.budget_ledger_id

    task = await db_session.get(AgentTask, task_id)
    task.objective = "Подменённая цель"
    await db_session.commit()
    retry = await client.post(f"/api/agent/tasks/{task_id}/run")

    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "agent_task_durable_migration_required"
    assert execute.await_count == 1
    await db_session.refresh(order)
    assert order.budget_ledger_id == ledger_id


@pytest.mark.asyncio
async def test_agent_task_foreign_owner_cannot_reuse_durable_binding(
    client: AsyncClient, monkeypatch
):
    from app.auth.jwt import get_current_user
    from app.auth.models import UserInfo, UserRole
    from app.main import app

    execute = AsyncMock(return_value=False)
    monkeypatch.setattr("app.tasks.work_orders.execute_work_order_now", execute)
    created = await client.post("/api/agent/tasks", json={"objective": "Owned work"})
    task_id = created.json()["id"]
    assert (await client.post(f"/api/agent/tasks/{task_id}/run")).status_code == 200

    bob = UserInfo(
        sub="bob-admin",
        email="bob@example.test",
        name="Bob",
        preferred_username="bob",
        roles=[UserRole.admin],
    )
    app.dependency_overrides[get_current_user] = lambda: bob
    try:
        denied = await client.post(f"/api/agent/tasks/{task_id}/run")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "agent_task_durable_migration_required"
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_agent_task_intake_failure_after_staged_graph_rolls_back_every_row(
    test_engine, monkeypatch
):
    from app.domain import agent_intake

    captured: dict[str, uuid.UUID] = {}
    execute = AsyncMock(side_effect=AssertionError("rejected intake must not execute"))
    monkeypatch.setattr("app.tasks.work_orders.execute_work_order_now", execute)
    async with _committed_api(test_engine, monkeypatch) as (api_client, factory):
        created = await api_client.post(
            "/api/agent/tasks", json={"objective": "Must roll back staged intake"}
        )
        task_id = uuid.UUID(created.json()["id"])
        real_submit = agent_intake.submit_agent_intake

        async def fail_after_real_submit(*args, **kwargs):
            result = await real_submit(*args, **kwargs)
            captured.update(
                {
                    "run": result.run.id,
                    "order": result.order.id,
                    "ledger": result.order.budget_ledger_id,
                    "message": result.run.user_message_id,
                    "session": result.run.session_id,
                }
            )
            raise agent_intake.IntakeValidationError("rejected after staged graph")

        monkeypatch.setattr(agent_intake, "submit_agent_intake", fail_after_real_submit)
        try:
            response = await api_client.post(f"/api/agent/tasks/{task_id}/run")

            assert response.status_code == 422
            assert set(captured) == {"run", "order", "ledger", "message", "session"}
            assert execute.await_count == 0
            async with factory() as verify:
                task = await verify.get(AgentTask, task_id)
                assert task.status == "created"
                assert (task.metadata_ or {}).get("work_order_id") is None
                assert await verify.get(DurableChatRun, captured["run"]) is None
                assert await verify.get(WorkOrder, captured["order"]) is None
                assert await verify.get(WorkBudgetLedger, captured["ledger"]) is None
                assert await verify.get(ChatMessage, captured["message"]) is None
                assert await verify.get(ChatSession, captured["session"]) is None
        finally:
            await _delete_committed_agent_task(factory, task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("owners", [("same-owner", "same-owner"), ("alice", "bob")])
async def test_agent_task_concurrent_posts_create_one_owned_durable_graph(
    test_engine, monkeypatch, owners
):
    execute = AsyncMock(return_value=False)
    monkeypatch.setattr("app.tasks.work_orders.execute_work_order_now", execute)
    async with _committed_api(test_engine, monkeypatch) as (api_client, factory):
        created = await api_client.post(
            "/api/agent/tasks", json={"objective": f"Concurrent task {uuid.uuid4()}"}
        )
        task_id = uuid.UUID(created.json()["id"])
        try:
            first, second = await asyncio.gather(
                api_client.post(
                    f"/api/agent/tasks/{task_id}/run",
                    headers={"x-test-owner": owners[0]},
                ),
                api_client.post(
                    f"/api/agent/tasks/{task_id}/run",
                    headers={"x-test-owner": owners[1]},
                ),
            )

            if owners[0] == owners[1]:
                assert first.status_code == second.status_code == 200
                assert (
                    first.json()["metadata"]["work_order_id"]
                    == second.json()["metadata"]["work_order_id"]
                )
            else:
                assert sorted([first.status_code, second.status_code]) == [200, 409]
                denied = first if first.status_code == 409 else second
                assert denied.json()["detail"]["code"] == "agent_task_durable_migration_required"
            assert execute.await_count == 1

            async with factory() as verify:
                orders = list(
                    (
                        await verify.scalars(
                            select(WorkOrder).where(WorkOrder.legacy_agent_task_id == task_id)
                        )
                    ).all()
                )
                assert len(orders) == 1
                order = orders[0]
                assert order.owner_key in set(owners)
                assert (
                    await verify.scalar(
                        select(func.count())
                        .select_from(DurableChatRun)
                        .where(DurableChatRun.work_order_id == order.id)
                    )
                    == 1
                )
                assert (
                    await verify.scalar(
                        select(func.count())
                        .select_from(WorkBudgetLedger)
                        .where(WorkBudgetLedger.root_work_order_id == order.id)
                    )
                    == 1
                )
        finally:
            await _delete_committed_agent_task(factory, task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["owner", "root"])
async def test_agent_task_retry_rejects_corrupt_ledger_binding(
    client: AsyncClient, db_session, monkeypatch, corruption
):
    execute = AsyncMock(return_value=False)
    monkeypatch.setattr("app.tasks.work_orders.execute_work_order_now", execute)
    created = await client.post(
        "/api/agent/tasks", json={"objective": f"Ledger integrity {corruption}"}
    )
    task_id = uuid.UUID(created.json()["id"])
    first = await client.post(f"/api/agent/tasks/{task_id}/run")
    assert first.status_code == 200
    order = await db_session.get(WorkOrder, uuid.UUID(first.json()["metadata"]["work_order_id"]))
    ledger = await db_session.get(WorkBudgetLedger, order.budget_ledger_id)
    if corruption == "owner":
        ledger.owner_key = "foreign-owner"
    else:
        foreign_root = WorkOrder(
            owner_key="dev-user",
            source="api",
            objective="Foreign ledger root",
        )
        db_session.add(foreign_root)
        await db_session.flush()
        ledger.root_work_order_id = foreign_root.id
    await db_session.commit()

    retry = await client.post(f"/api/agent/tasks/{task_id}/run")

    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "agent_task_durable_migration_required"
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_agent_task_historic_binding_requires_explicit_migration(
    client: AsyncClient, db_session, monkeypatch
):
    from app.domain.work_orders import create_single_step_plan, create_work_order

    task = AgentTask(objective="Historic work", role="worker", status="created")
    db_session.add(task)
    await db_session.flush()
    order = await create_work_order(
        db_session,
        owner_key="dev-user",
        objective=task.objective,
        source="legacy_agent_task",
        legacy_agent_task_id=task.id,
    )
    await create_single_step_plan(
        db_session,
        order,
        kind="agent_turn",
        title="Historic headless step",
        input_data={"prompt": task.objective},
    )
    await db_session.commit()
    execute = AsyncMock(side_effect=AssertionError("historic work must not replay"))
    monkeypatch.setattr("app.tasks.work_orders.execute_work_order_now", execute)

    response = await client.post(f"/api/agent/tasks/{task.id}/run")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "agent_task_durable_migration_required"
    assert execute.await_count == 0
    await db_session.refresh(task)
    assert task.status == "created"
    assert order.budget_ledger_id is None


@pytest.mark.asyncio
async def test_agent_task_approval_pause_stays_nonterminal_and_does_not_restart(
    client: AsyncClient, monkeypatch
):
    from app.ai.agent_loop import AgentSession

    class ApprovalAgent:
        def __init__(self, send):
            self._executor = AgentSession(send)

        def hydrate_history(self, history):
            self._executor.hydrate_history(history)

        async def on_user_message(self, prompt, **kwargs):
            await self._executor._request_approval("email.send", {"draft_id": "draft-1"})

    monkeypatch.setattr("app.ai.orchestrator.AgentOrchestrator", ApprovalAgent)
    created = await client.post(
        "/api/agent/tasks",
        json={"objective": "Подготовить действие с подтверждением"},
    )
    task_id = created.json()["id"]

    first = await client.post(f"/api/agent/tasks/{task_id}/run")

    assert first.status_code == 200
    assert first.json()["status"] == "running"
    assert first.json()["metadata"]["run_status"] == "blocked"
    assert first.json()["metadata"]["work_order_status"] == "blocked"
    assert first.json()["metadata"].get("run_finished_at") is None
    second = await client.post(f"/api/agent/tasks/{task_id}/run")
    assert second.status_code == 200
    assert second.json()["status"] == "running"
    assert second.json()["metadata"]["work_order_id"] == first.json()["metadata"]["work_order_id"]


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
    resp = await client.post(
        "/api/agent/teams",
        json={
            "name": "Финансовый отдел",
            "purpose": "Контроль платежей и договоров",
        },
    )
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
async def test_create_agent_cron(client: AsyncClient, db_session):
    grant = DelegationGrant(
        owner_key="dev-user",
        title="Расписание сводки",
        actions=["agent.cron.run"],
        constraints={
            "schedule": "0 18 * * 5",
            "prompt_sha256": hashlib.sha256(
                "Сформируй сводку по аномалиям за неделю".encode()
            ).hexdigest(),
        },
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(grant)
    await db_session.commit()
    resp = await client.post(
        "/api/agent/cron",
        json={
            "schedule": "0 18 * * 5",
            "prompt": "Сформируй сводку по аномалиям за неделю",
            "delegation_grant_id": str(grant.id),
            "description": "Еженедельный пятничный отчёт",
        },
    )
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
    resp = await client.patch(
        f"/api/agent/cron/{agent_cron.id}",
        json={
            "enabled": False,
            "description": "Временно отключено",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False


# ── Plugins ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_agent_plugin(client: AsyncClient):
    resp = await client.post(
        "/api/agent/plugins",
        json={
            "plugin_key": "test-plugin-001",
            "name": "Тестовый плагин",
            "version": "0.1.0",
            "description": "Плагин для тестирования",
            "manifest": {"tools": [], "permissions": []},
            "risk_level": "low",
        },
    )
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
    resp = await client.post(
        "/api/agent/config/proposals",
        json={
            "setting_path": "model",
            "proposed_value": "qwen3.5:14b",
            "reason": "Тест производительности на новой модели",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data


@pytest.mark.asyncio
async def test_list_config_proposals(client: AsyncClient):
    resp = await client.get("/api/agent/config/proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


# ── Ф7 (AGENT_AUTONOMY_ROADMAP.md): agent_tone is intentionally not protected ──


@pytest.fixture
def _isolated_agent_config(tmp_path, monkeypatch):
    """save_builtin_agent_config writes to Redis + a JSON file (see
    app/ai/agent_config.py) — neither is per-test-transaction-isolated like
    db_session, so a real change here would leak into every later test in
    the same run. Stubs both with a throwaway in-memory dict / tmp file."""
    from app.ai import agent_config as ac

    store: dict = {}
    monkeypatch.setattr(ac, "_redis_get_agent_config", lambda: dict(store) if store else None)

    def _set(data: dict) -> None:
        store.clear()
        store.update(data)

    monkeypatch.setattr(ac, "_redis_set_agent_config", _set)
    monkeypatch.setattr(ac, "_CONFIG_FILE", tmp_path / "agent_config.json")
    yield


@pytest.mark.asyncio
async def test_agent_tone_proposal_applies_immediately_without_approval(
    client: AsyncClient, _isolated_agent_config
):
    resp = await client.post(
        "/api/agent/config/proposals",
        json={
            "setting_path": "agent_tone",
            "proposed_value": "friendly",
            "reason": "Тест Ф7: тон не защищённая настройка",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["protected"] is False
    assert data["status"] == "applied"

    from app.ai.agent_config import get_builtin_agent_config

    assert get_builtin_agent_config().agent_tone == "friendly"


@pytest.mark.asyncio
async def test_system_prompt_proposal_still_requires_protected_flow(
    client: AsyncClient, _isolated_agent_config
):
    """Regression guard: Ф7 only carves out agent_tone — system_prompt (and
    every other PROTECTED_SETTINGS entry) must still land as a pending
    proposal, not apply itself."""
    resp = await client.post(
        "/api/agent/config/proposals",
        json={
            "setting_path": "system_prompt",
            "proposed_value": "Ты — другой агент.",
            "reason": "Regression test",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["protected"] is True
    assert data["status"] == "pending"

    from app.ai.agent_config import get_builtin_agent_config

    assert get_builtin_agent_config().system_prompt != "Ты — другой агент."
