"""Atomic recipient commit, lost-response lookup and fenced duplicate delivery."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.agent_control_plane import AgentTaskPropose, propose_agent_task_tool
from app.api.chat_runs import (
    ChatRunCreate,
    get_chat_action,
    submit_chat_run,
)
from app.auth.jwt import _DEV_USER, get_current_user
from app.auth.models import UserRole
from app.db.agent_runtime_models import ChatLogicalAction
from app.db.models import AgentTask, AgentTeam, WorkEvent, WorkOrder, WorkStep, WorkStepAttempt
from app.domain.action_receipts import verify_proposal_receipt
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


async def proposal(factory, user=_DEV_USER):
    payload = AgentTaskPropose(objective=f"Receipt test {uuid.uuid4()}")
    async with factory() as db:
        run = await submit_chat_run(
            ChatRunCreate(request_id=uuid.uuid4(), content=f"Receipt scenario {uuid.uuid4()}"),
            db,
            user,
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


@pytest.mark.asyncio
async def test_verification_matched_is_a_read_only_content_snapshot(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, _, key = await proposal(factory)
    committed = await deliver(factory, payload, key)

    async with factory() as db:
        before_events = await db.scalar(
            select(func.count())
            .select_from(WorkEvent)
            .where(WorkEvent.work_order_id == run["work_order_id"])
        )
        before_task = await db.scalar(
            select(AgentTask).where(AgentTask.objective == payload.objective)
        )
        before_values = {
            "id": before_task.id,
            "objective": before_task.objective,
            "description": before_task.description,
            "role": before_task.role,
            "status": before_task.status,
            "team_id": before_task.team_id,
            "output": before_task.output,
            "metadata_": before_task.metadata_,
        }
        result = await verify_proposal_receipt(
            db, await db.get(ChatLogicalAction, action_id), _DEV_USER
        )
        after_events = await db.scalar(
            select(func.count())
            .select_from(WorkEvent)
            .where(WorkEvent.work_order_id == run["work_order_id"])
        )
        after_task = await db.get(AgentTask, before_task.id, populate_existing=True)

    assert result["status"] == "matched"
    assert result["action_id"] == str(action_id)
    assert result["artifact_id"] == committed["id"]
    assert result["receipt_response_digest"] == digest(committed)
    assert result["expected_content_digest"] == result["current_content_digest"]
    assert result["checked_fields"] == [
        "id",
        "objective",
        "description",
        "role",
        "status",
        "team_id",
        "output",
        "metadata",
    ]
    assert result["can_replay"] is False
    assert result["can_resume"] is False
    assert after_events == before_events
    assert {
        "id": after_task.id,
        "objective": after_task.objective,
        "description": after_task.description,
        "role": after_task.role,
        "status": after_task.status,
        "team_id": after_task.team_id,
        "output": after_task.output,
        "metadata_": after_task.metadata_,
    } == before_values


@pytest.mark.asyncio
async def test_verification_changed_detects_each_mutable_content_field(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, _, key = await proposal(factory)
    committed = await deliver(factory, payload, key)
    task_id = uuid.UUID(committed["id"])
    async with factory() as db:
        task = await db.get(AgentTask, task_id)
        original = {
            "objective": task.objective,
            "description": task.description,
            "role": task.role,
            "status": task.status,
            "team_id": task.team_id,
            "output": task.output,
            "metadata_": task.metadata_,
        }
        event_count = await db.scalar(
            select(func.count())
            .select_from(WorkEvent)
            .where(WorkEvent.work_order_id == run["work_order_id"])
        )
        team = AgentTeam(name=f"Verification team {uuid.uuid4()}")
        db.add(team)
        await db.flush()
        changed_values = {
            "objective": "Changed objective",
            "description": "Changed description",
            "role": "changed-role",
            "status": "changed-status",
            "team_id": team.id,
            "output": "Changed output",
            "metadata_": {"changed": True},
        }
        await db.commit()

    for field, changed_value in changed_values.items():
        async with factory() as db:
            task = await db.get(AgentTask, task_id)
            setattr(task, field, changed_value)
            await db.commit()

        async with factory() as db:
            action = await db.get(ChatLogicalAction, action_id)
            result = await verify_proposal_receipt(db, action, _DEV_USER)
            assert result["status"] == "changed", field
            assert result["expected_content_digest"] != result["current_content_digest"]

        async with factory() as db:
            task = await db.get(AgentTask, task_id)
            setattr(task, field, original[field])
            await db.commit()

    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkEvent)
                .where(WorkEvent.work_order_id == run["work_order_id"])
            )
            == event_count
        )


@pytest.mark.asyncio
async def test_verification_missing_task_and_receipt_are_inconclusive(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    payload, run, action_id, _, _ = await proposal(factory)
    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        result = await verify_proposal_receipt(db, action, _DEV_USER)
    assert result["status"] == "inconclusive"
    assert result["reason"] == "recipient_receipt_missing"
    assert result["can_replay"] is False
    assert result["can_resume"] is False

    payload, run, action_id, _, key = await proposal(factory)
    committed = await deliver(factory, payload, key)
    async with factory() as db:
        await db.delete(await db.get(AgentTask, uuid.UUID(committed["id"])))
        await db.commit()
        action = await db.get(ChatLogicalAction, action_id)
        result = await verify_proposal_receipt(db, action, _DEV_USER)
    assert result["status"] == "missing"
    assert result["artifact_id"] == committed["id"]
    assert result["current_content_digest"] is None
    assert result["can_replay"] is False
    assert result["can_resume"] is False


@pytest.mark.asyncio
async def test_verification_rejects_corrupt_receipt_and_unknown_recipient(test_engine):
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
        action = await db.get(ChatLogicalAction, action_id)
        with pytest.raises(HTTPException, match="integrity mismatch"):
            await verify_proposal_receipt(db, action, _DEV_USER)

    payload, run, action_id, _, key = await proposal(factory)
    await deliver(factory, payload, key)
    async with factory() as db:
        event = await db.scalar(
            select(WorkEvent).where(
                WorkEvent.work_order_id == run["work_order_id"],
                WorkEvent.event_type == "chat.recipient_committed",
            )
        )
        event.payload = {**event.payload, "operation": "other.recipient"}
        await db.commit()
        action = await db.get(ChatLogicalAction, action_id)
        result = await verify_proposal_receipt(db, action, _DEV_USER)
    assert result["status"] == "inconclusive"
    assert result["reason"] == "unsupported_recipient"
    assert "artifact_id" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_kind", ["invalid_uuid", "missing_id"])
async def test_verification_rejects_malformed_artifact_binding(test_engine, malformed_kind):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, _, key = await proposal(factory)
    committed = await deliver(factory, payload, key)
    async with factory() as db:
        event_row = await db.scalar(
            select(WorkEvent).where(
                WorkEvent.work_order_id == run["work_order_id"],
                WorkEvent.event_type == "chat.recipient_committed",
            )
        )
        original = event_row.payload["response"]
        if malformed_kind == "invalid_uuid":
            malformed = {**original, "id": "not-a-uuid"}
        else:
            malformed = {name: value for name, value in original.items() if name != "id"}
        event_row.payload = {
            **event_row.payload,
            "response": malformed,
            "response_digest": digest(malformed),
        }
        await db.commit()
        action = await db.get(ChatLogicalAction, action_id)
        with pytest.raises(HTTPException, match="artifact binding is invalid"):
            await verify_proposal_receipt(db, action, _DEV_USER)
    assert committed["id"] == original["id"]


@pytest.mark.asyncio
async def test_verification_route_is_owner_scoped_and_requires_current_admin(
    client, db_session, test_engine
):
    from app.main import app

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    alice = _DEV_USER.model_copy(update={"sub": "alice"})
    payload, run, action_id, _, key = await proposal(factory, alice)
    await deliver(factory, payload, key, alice)
    url = f"/api/agent/chat-runs/{run['id']}/actions/{action_id}/verification"

    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        order = await db.get(WorkOrder, run["work_order_id"])
        attempt = await db.get(WorkStepAttempt, action.attempt_id)
        step = await db.get(WorkStep, attempt.step_id)
        before_state = {
            "order": (order.status, order.blocker, order.plan_revision, order.metadata_),
            "action": (action.status, action.result, action.result_digest),
            "attempt": (attempt.status, attempt.checkpoint, attempt.heartbeat_at),
            "step": (step.state, step.lease_owner, step.lease_expires_at),
        }
        before_events = await db.scalar(
            select(func.count())
            .select_from(WorkEvent)
            .where(WorkEvent.work_order_id == run["work_order_id"])
        )

    app.dependency_overrides[get_current_user] = lambda: alice
    assert not db_session.dirty
    assert not db_session.new
    assert not db_session.deleted
    statements = []

    def capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE"}:
            statements.append(verb)

    event.listen(test_engine.sync_engine, "before_cursor_execute", capture_sql)
    try:
        response = await client.get(url)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", capture_sql)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "matched"
    assert body["can_replay"] is False
    assert body["can_resume"] is False
    assert statements == []
    assert not db_session.dirty
    assert not db_session.new
    assert not db_session.deleted

    bob = alice.model_copy(update={"sub": "bob"})
    app.dependency_overrides[get_current_user] = lambda: bob
    assert (await client.get(url)).status_code == 404

    viewer = alice.model_copy(update={"roles": [UserRole.viewer]})
    app.dependency_overrides[get_current_user] = lambda: viewer
    assert (await client.get(url)).status_code == 403

    app.dependency_overrides[get_current_user] = lambda: alice
    missing_action = f"/api/agent/chat-runs/{run['id']}/actions/{uuid.uuid4()}/verification"
    assert (await client.get(missing_action)).status_code == 404

    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        order = await db.get(WorkOrder, run["work_order_id"])
        attempt = await db.get(WorkStepAttempt, action.attempt_id)
        step = await db.get(WorkStep, attempt.step_id)
        after_state = {
            "order": (order.status, order.blocker, order.plan_revision, order.metadata_),
            "action": (action.status, action.result, action.result_digest),
            "attempt": (attempt.status, attempt.checkpoint, attempt.heartbeat_at),
            "step": (step.state, step.lease_owner, step.lease_expires_at),
        }
        after_events = await db.scalar(
            select(func.count())
            .select_from(WorkEvent)
            .where(WorkEvent.work_order_id == run["work_order_id"])
        )
    assert after_state == before_state
    assert after_events == before_events
