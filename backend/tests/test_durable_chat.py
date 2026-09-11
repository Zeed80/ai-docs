"""Durable intake, ownership, event replay and conservative worker-loss behavior."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.chat_runs import ChatRunCreate, submit_chat_run
from app.auth.jwt import _DEV_USER
from app.db.agent_runtime_models import DurableChatRun
from app.db.models import ChatMessage, WorkEvent, WorkOrder, WorkStep
from app.domain.work_orders import claim_ready_step, reclaim_expired_leases
from app.tasks.durable_chat import run_durable_chat


def request(**kwargs):
    return {"request_id": str(uuid.uuid4()), "content": "Return a short answer", **kwargs}


@pytest.mark.asyncio
async def test_intake_idempotency_and_conflicts(client, db_session):
    body = request()
    first = await client.post("/api/agent/chat-runs", json=body)
    assert first.status_code == 202, first.text
    run = first.json()
    retry = await client.post("/api/agent/chat-runs", json=body)
    assert retry.json() == run
    conflict = await client.post("/api/agent/chat-runs", json={**body, "content": "Different"})
    assert conflict.status_code == 409
    busy = await client.post("/api/agent/chat-runs", json=request(session_id=run["session_id"]))
    assert busy.status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(DurableChatRun)) == 1
    step = await db_session.scalar(
        select(WorkStep).where(WorkStep.work_order_id == uuid.UUID(run["work_order_id"]))
    )
    assert step.max_attempts == 1
    order = await db_session.get(WorkOrder, uuid.UUID(run["work_order_id"]))
    assert order.budgets["max_replans"] == 0
    assert (await client.post(f"/api/work-orders/{run['work_order_id']}/run")).status_code == 409
    assert (
        await client.post(
            f"/api/work-orders/{run['work_order_id']}/instructions",
            json={"instruction": "Repeat the action"},
        )
    ).status_code == 409
    events = (await client.get(f"/api/agent/chat-runs/{run['id']}/events?limit=1")).json()
    assert len(events["items"]) == 1
    cursor = events["next_cursor"]
    later = (await client.get(f"/api/agent/chat-runs/{run['id']}/events?after={cursor}")).json()
    assert all(e["sequence"] > cursor for e in later["items"])


@pytest.mark.asyncio
async def test_foreign_owner_and_service_are_denied(client):
    from app.auth.jwt import get_current_user
    from app.main import app

    run = (await client.post("/api/agent/chat-runs", json=request())).json()
    original = app.dependency_overrides.copy()
    try:
        app.dependency_overrides[get_current_user] = lambda: _DEV_USER.model_copy(
            update={"sub": "other"}
        )
        assert (await client.get(f"/api/agent/chat-runs/{run['id']}")).status_code == 404
        assert (await client.get(f"/api/agent/chat-runs/{run['id']}/events")).status_code == 404
        assert (
            await client.post("/api/agent/chat-runs", json=request(session_id=run["session_id"]))
        ).status_code == 404
        app.dependency_overrides[get_current_user] = lambda: _DEV_USER.model_copy(
            update={"via_agent": True}
        )
        assert (await client.post("/api/agent/chat-runs", json=request())).status_code == 403
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


@pytest.mark.asyncio
async def test_concurrent_retry_has_one_committed_request(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    body = ChatRunCreate(**request())

    async def submit():
        async with factory() as db:
            return await submit_chat_run(body, db, _DEV_USER)

    a, b = await asyncio.gather(submit(), submit())
    assert a["id"] == b["id"]
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(DurableChatRun)
                .where(DurableChatRun.request_id == body.request_id)
            )
            == 1
        )


async def claimed_run(factory):
    async with factory() as db:
        run = await submit_chat_run(ChatRunCreate(**request()), db, _DEV_USER)
    async with factory() as db:
        order, step, attempt = await claim_ready_step(
            db, worker_id="chat-test", work_order_id=run["work_order_id"]
        )
        await db.commit()
        return run, step.id, attempt.id


class FakeAgent:
    def __init__(self, send):
        self.send = send
        self._executor = SimpleNamespace(total_tokens=12)

    def hydrate_history(self, history):
        self.history = history

    async def on_user_message(self, prompt, **kwargs):
        await self.send({"type": "text", "content": "Answer"})
        await self.send({"type": "done"})


@pytest.mark.asyncio
async def test_worker_persists_result_without_http_connection(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step, attempt = await claimed_run(factory)
    result = await run_durable_chat(
        run["work_order_id"], step, attempt, session_factory=factory, agent_factory=FakeAgent
    )
    assert result["text"] == "Answer"
    async with factory() as db:
        saved = await db.get(DurableChatRun, run["id"])
        message = await db.get(ChatMessage, saved.result_message_id)
        assert message.content == "Answer"
        assert message.metadata_["verified"] is False
        assert await db.scalar(
            select(WorkEvent.id).where(
                WorkEvent.work_order_id == run["work_order_id"],
                WorkEvent.event_type == "chat.response_saved",
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["approval", "error", "expired", "canceled", "budget"])
async def test_fail_closed_before_effect_or_result(test_engine, mode):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, attempt_id = await claimed_run(factory)
    effects = []
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        if mode == "expired":
            step = await db.get(WorkStep, step_id)
            step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        if mode == "canceled":
            order.status = "canceled"
        if mode == "budget":
            order.budgets = {**order.budgets, "max_tool_calls": 0}
        await db.commit()

    class Agent(FakeAgent):
        async def on_user_message(self, prompt, **kwargs):
            if mode == "approval":
                await self._executor._request_approval("invoices.approve", {"id": "one"})
            elif mode == "error":
                await self.send({"type": "error", "error_code": "provider_down"})
                await self.send({"type": "text", "content": "Fallback text"})
                return
            await self.send({"type": "tool_call", "tool": "test"})
            effects.append("effect")

    with pytest.raises(RuntimeError):
        await run_durable_chat(
            run["work_order_id"], step_id, attempt_id, session_factory=factory, agent_factory=Agent
        )
    assert effects == []
    async with factory() as db:
        saved = await db.get(DurableChatRun, run["id"])
        assert saved.result_message_id is None


@pytest.mark.asyncio
async def test_worker_loss_blocks_instead_of_replaying(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, _ = await claimed_run(factory)
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    async with factory() as db:
        await reclaim_expired_leases(db)
        await db.commit()
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        assert order.status == "blocked"
        assert await claim_ready_step(db, worker_id="replacement", work_order_id=order.id) is None


@pytest.mark.asyncio
async def test_event_is_committed_before_tool_effect(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step, attempt = await claimed_run(factory)

    class Agent(FakeAgent):
        async def on_user_message(self, prompt, **kwargs):
            assert self._executor._session_id == str(run["session_id"])
            assert self.history == []
            await self.send({"type": "tool_call", "tool": "test.read", "args": {}})
            async with factory() as db:
                assert await db.scalar(
                    select(WorkEvent.id).where(
                        WorkEvent.work_order_id == run["work_order_id"],
                        WorkEvent.event_type == "chat.tool_call",
                    )
                )
            await super().on_user_message(prompt, **kwargs)

    await run_durable_chat(
        run["work_order_id"], step, attempt, session_factory=factory, agent_factory=Agent
    )


@pytest.mark.asyncio
async def test_persistence_failure_crosses_model_recovery_handlers(test_engine, monkeypatch):
    from app.tasks import durable_chat

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step, attempt = await claimed_run(factory)
    effects = []

    async def broken(*args, **kwargs):
        raise ConnectionError("Database unavailable")

    monkeypatch.setattr(durable_chat, "append_event", broken)

    class Agent(FakeAgent):
        async def on_user_message(self, prompt, **kwargs):
            try:
                await self.send({"type": "tool_call", "tool": "test"})
            except Exception:
                effects.append("unsafe recovery")
            effects.append("effect")

    with pytest.raises(RuntimeError, match="persistence failed"):
        await run_durable_chat(
            run["work_order_id"], step, attempt, session_factory=factory, agent_factory=Agent
        )
    assert effects == []


@pytest.mark.asyncio
async def test_worker_dispatch_settles_durable_turn(test_engine, monkeypatch):
    from unittest.mock import AsyncMock

    from app.ai import orchestrator
    from app.tasks import work_orders

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step, attempt = await claimed_run(factory)
    monkeypatch.setattr(orchestrator, "AgentOrchestrator", FakeAgent)
    verifier = AsyncMock()
    monkeypatch.setattr(work_orders, "verify_completed_step", verifier)
    assert await work_orders.execute_claimed_step(
        step, attempt, schedule_verification=False, session_factory=factory
    )
    verifier.assert_awaited_once()
    assert not await work_orders.execute_claimed_step(
        step, attempt, schedule_verification=False, session_factory=factory
    )
    async with factory() as db:
        saved = await db.get(WorkStep, step)
        assert saved.state == "succeeded"
        assert saved.output["text"] == "Answer"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.session_id == run["session_id"], ChatMessage.role == "assistant")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_durable_schema_migration_round_trip(db_session):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    path = Path(__file__).resolve().parents[1] / "migrations/versions/20260911_0001_durable_chat.py"
    spec = importlib.util.spec_from_file_location("durable_chat_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "chat_migration_" + uuid.uuid4().hex
    connection = await db_session.connection()
    await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    await connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))

    def verify(sync):
        module.op = Operations(MigrationContext.configure(sync))
        module.upgrade()
        assert inspect(sync).get_table_names(schema=schema) == ["durable_chat_runs"]
        assert len(inspect(sync).get_foreign_keys("durable_chat_runs", schema=schema)) == 4
        module.downgrade()
        assert not inspect(sync).get_table_names(schema=schema)

    await connection.run_sync(verify)
