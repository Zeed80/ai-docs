"""Atomic recipient commit, lost-response lookup and fenced duplicate delivery."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.agent_control_plane import AgentTaskPropose, propose_agent_task_tool
from app.api.chat_runs import ChatRunCreate, get_chat_action, submit_chat_run
from app.auth.jwt import _DEV_USER
from app.db.agent_runtime_models import ChatLogicalAction
from app.db.models import AgentTask, WorkEvent, WorkOrder, WorkStep
from app.domain.chat_action_journal import digest, record_boundary
from app.domain.work_orders import claim_ready_step


@pytest_asyncio.fixture(autouse=True)
async def settle_receipt_test_orders(test_engine):
    # These integration tests commit across independent connections. Leave no
    # active receipt-test orders for subsequent global budget/reaper tests.
    yield
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        await db.execute(
            update(WorkOrder)
            .where(
                WorkOrder.objective.startswith("Receipt scenario "),
            )
            .values(status="blocked")
        )
        await db.commit()


async def proposal(factory):
    payload = AgentTaskPropose(objective=f"Receipt test {uuid.uuid4()}")
    async with factory() as db:
        run = await submit_chat_run(
            ChatRunCreate(request_id=uuid.uuid4(), content=f"Receipt scenario {uuid.uuid4()}"),
            db,
            _DEV_USER,
        )
    async with factory() as db:
        order, step, attempt = await claim_ready_step(
            db,
            worker_id="receipt-worker",
            work_order_id=run["work_order_id"],
        )
        action_id = uuid.uuid4()
        call = {
            "id": "call-proposal",
            "function": {
                "name": "agent_control",
                "arguments": json.dumps(
                    {
                        "action": "task_propose",
                        "reason": "Test",
                        "body": payload.model_dump(mode="json"),
                    }
                ),
            },
        }
        boundary = {
            "phase": "tools_planned",
            "pending_calls": [call],
            "action_ids": {call["id"]: str(action_id)},
            "in_flight_call_id": None,
        }
        await record_boundary(db, order, attempt, boundary)
        await record_boundary(
            db,
            order,
            attempt,
            {**boundary, "phase": "tool_started", "in_flight_call_id": call["id"]},
        )
        await db.commit()
        return payload, run, action_id, step.id, f"{action_id}:{attempt.id}"


async def deliver(factory, payload, key, user=_DEV_USER):
    async with factory() as db:
        return await propose_agent_task_tool(payload, db, user, key)


@pytest.mark.asyncio
async def test_concurrent_duplicate_and_lost_response_have_one_commit(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, _, key = await proposal(factory)
    first, second = await asyncio.gather(
        deliver(factory, payload, key), deliver(factory, payload, key)
    )
    assert first == second
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AgentTask)
                .where(AgentTask.objective == payload.objective)
            )
            == 1
        )
        action = await db.get(ChatLogicalAction, action_id)
        assert action.result is None  # Simulate lost HTTP response/checkpoint.
        order = await db.get(WorkOrder, run["work_order_id"], with_for_update=True)
        order.status = "blocked"
        await db.commit()
    async with factory() as db:
        detail = await get_chat_action(run["id"], action_id, db, _DEV_USER)
        assert detail["status"] == "outcome_unknown"
        assert detail["recipient_receipt"]["response"] == first
        assert detail["recipient_receipt"]["evidence_scope"] == "database_commit"
        assert detail["can_replay"] is False
    assert await deliver(factory, payload, key) == first  # Receipt only, no new effect.


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_domain_mutation(test_engine, monkeypatch):
    from app.domain import action_receipts

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, _, _, key = await proposal(factory)
    real = action_receipts.record_proposal_receipt

    async def fail_after_insert(*args):
        await real(*args)
        raise RuntimeError("Crash before commit")

    monkeypatch.setattr(action_receipts, "record_proposal_receipt", fail_after_insert)
    with pytest.raises(RuntimeError, match="Crash before commit"):
        await deliver(factory, payload, key)
    async with factory() as db:
        assert not await db.scalar(
            select(AgentTask.id).where(AgentTask.objective == payload.objective)
        )
        action = await db.get(ChatLogicalAction, uuid.UUID(key.split(":")[0]))
        assert await action_receipts.read_receipt(db, action) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "violation",
    [
        "owner",
        "arguments",
        "operation",
        "digest",
        "attempt",
        "expired",
        "canceled",
        "budget",
        "recorded",
        "invalid_key",
    ],
)
async def test_recipient_rejects_unbound_or_fenced_effect(test_engine, violation):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, step_id, key = await proposal(factory)
    user = _DEV_USER
    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        order = await db.get(WorkOrder, run["work_order_id"])
        if violation == "owner":
            user = user.model_copy(update={"sub": "another-owner"})
        elif violation == "arguments":
            payload = payload.model_copy(update={"objective": "Changed"})
        elif violation == "operation":
            action.request = {**action.request, "name": "other_tool"}
            action.request_digest = digest(action.request)
        elif violation == "digest":
            action.request_digest = "0" * 64
        elif violation == "attempt":
            key = f"{action_id}:{uuid.uuid4()}"
        elif violation == "expired":
            (await db.get(WorkStep, step_id)).lease_expires_at = datetime.now(UTC) - timedelta(
                seconds=1
            )
        elif violation == "canceled":
            order.status = "canceled"
        elif violation == "budget":
            order.started_at = datetime.now(UTC) - timedelta(days=1)
        elif violation == "recorded":
            action.status = "outcome_unknown"
        else:
            key = "not-a-journal-key"
        await db.commit()
    with pytest.raises(HTTPException) as exc:
        await deliver(factory, payload, key, user)
    assert exc.value.status_code in {400, 404, 409}


@pytest.mark.asyncio
async def test_corrupt_receipt_is_not_reused(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, _, key = await proposal(factory)
    await deliver(factory, payload, key)
    async with factory() as db:
        event = await db.scalar(
            select(WorkEvent).where(
                WorkEvent.work_order_id == run["work_order_id"],
                WorkEvent.event_type == "chat.recipient_committed",
            )
        )
        event.payload = {**event.payload, "response": {"id": "corrupt"}}
        await db.commit()
    with pytest.raises(HTTPException, match="integrity mismatch"):
        await deliver(factory, payload, key)


@pytest.mark.asyncio
async def test_legacy_proposal_without_key_remains_available(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    result = await deliver(factory, AgentTaskPropose(objective="Legacy proposal"), None)
    assert result.status == "proposed"


@pytest.mark.asyncio
@pytest.mark.parametrize("dict_arguments", [False, True])
async def test_gateway_forwards_key_to_real_recipient_route(
    test_engine, monkeypatch, dict_arguments
):
    from fastapi import FastAPI

    from app.api import agent_control_plane, capability_router
    from app.db.session import get_db

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, _, key = await proposal(factory)
    if dict_arguments:
        async with factory() as db:
            action = await db.get(ChatLogicalAction, action_id)
            action.request = {
                **action.request,
                "arguments": json.loads(action.request["arguments"]),
            }
            action.request_digest = digest(action.request)
            await db.commit()
    app = FastAPI()
    app.include_router(agent_control_plane.router, prefix="/api/agent")

    async def database():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        capability_router.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.ASGITransport(app=app),
            **kwargs,
        ),
    )
    monkeypatch.setattr(capability_router, "_service_headers", lambda actor: {})
    result = await capability_router._proxy(
        "POST",
        "/api/agent/tasks/propose",
        [],
        payload.model_dump(mode="json"),
        "http://recipient",
        acting_user=_DEV_USER.sub,
        idempotency_key=key,
    )
    async with factory() as db:
        detail = await get_chat_action(run["id"], action_id, db, _DEV_USER)
        assert detail["recipient_receipt"]["response"] == result
    assert result["status"] == "proposed"
