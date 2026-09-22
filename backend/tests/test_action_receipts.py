"""Atomic recipient commit, lost-response lookup and fenced duplicate delivery."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.agent_control_plane import AgentTaskPropose, propose_agent_task_tool
from app.api.chat_runs import (
    ChatRunCreate,
    get_chat_action,
    submit_chat_run,
)
from app.auth.jwt import _DEV_USER, get_current_user
from app.auth.models import UserRole
from app.db.agent_runtime_models import ActionReceipt, ChatLogicalAction
from app.db.models import (
    AgentTask,
    AgentTeam,
    InventoryItem,
    WorkAcceptanceCriterion,
    WorkArtifact,
    WorkEvent,
    WorkEvidence,
    WorkOrder,
    WorkStep,
    WorkStepAttempt,
)
from app.domain.action_receipts import read_receipt, verify_proposal_receipt
from app.domain.artifact_verification import (
    supported_artifact_verifiers,
    validate_artifact_verdict,
    verdict_proves_current_artifact,
    verify_action_artifact,
)
from app.domain.chat_action_journal import digest, record_boundary
from app.domain.work_orders import append_event, claim_ready_step


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
        # Warehouse recipient scenarios use independently committed sessions
        # to exercise the real duplicate-delivery race.  Keep their synthetic
        # rows out of unrelated warehouse API tests that share this schema.
        await db.execute(
            delete(InventoryItem).where(InventoryItem.name.startswith("Receipt item "))
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


async def warehouse_update_proposal(factory, *, item_id=None, payload=None, user=_DEV_USER):
    """Create one started, journaled warehouse.update_item call."""
    payload = payload or {"location": f"Receipt shelf {uuid.uuid4()}"}
    async with factory() as db:
        if item_id is None:
            item = InventoryItem(name=f"Receipt item {uuid.uuid4()}", unit="pcs", current_qty=1)
            db.add(item)
            await db.flush()
            item_id = item.id
        else:
            item = await db.get(InventoryItem, item_id)
        await db.commit()

    async with factory() as db:
        run = await submit_chat_run(
            ChatRunCreate(request_id=uuid.uuid4(), content=f"Receipt scenario {uuid.uuid4()}"),
            db,
            user,
        )
    async with factory() as db:
        order, _, attempt = await claim_ready_step(
            db,
            worker_id="warehouse-receipt-worker",
            work_order_id=run["work_order_id"],
        )
        action_id = uuid.uuid4()
        call = {
            "id": "call-warehouse-update",
            "function": {
                "name": "warehouse",
                "arguments": json.dumps(
                    {"action": "update_item", "item_id": str(item_id), **payload}
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
    return payload, run, action_id, item_id, f"{action_id}:{attempt.id}"


async def deliver_warehouse(factory, item_id, payload, key, user=_DEV_USER):
    from app.api.warehouse import InventoryItemUpdate, update_inventory_item

    async with factory() as db:
        return await update_inventory_item(
            item_id,
            InventoryItemUpdate(**payload),
            db,
            user,
            key,
        )


async def deliver_warehouse_asgi(factory, item_id, payload, key, user=_DEV_USER):
    """Exercise the actual warehouse route with independent DB connections."""
    from app.api import warehouse
    from app.db.session import get_db

    app = FastAPI()
    app.include_router(warehouse.router, prefix="/api/warehouse")

    async def database():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user
    headers = {"Idempotency-Key": key} if key is not None else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://warehouse-recipient"
    ) as client:
        return await client.patch(
            f"/api/warehouse/inventory/{item_id}", json=payload, headers=headers
        )


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
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ActionReceipt)
                .where(ActionReceipt.logical_action_id == action_id)
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
async def test_committed_action_accepts_source_or_current_attempt_for_read_only_retry(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, action_id, step_id, old_key = await proposal(factory)
    committed = await deliver(factory, payload, old_key)
    async with factory() as db:
        next_attempt = WorkStepAttempt(
            step_id=step_id,
            attempt_no=2,
            worker_id="replacement-worker",
            status="running",
        )
        db.add(next_attempt)
        await db.flush()
        action = await db.get(ChatLogicalAction, action_id)
        action.attempt_id = next_attempt.id
        await db.commit()
        new_key = f"{action_id}:{next_attempt.id}"

    assert await deliver(factory, payload, old_key) == committed
    assert await deliver(factory, payload, new_key) == committed
    with pytest.raises(HTTPException, match="attempt mismatch"):
        await deliver(factory, payload, f"{action_id}:{uuid.uuid4()}")
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AgentTask)
                .where(AgentTask.objective == payload.objective)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_cancellation_and_recipient_share_order_lock(test_engine, monkeypatch):
    from app.api import work_orders as work_order_api

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, _, _, key = await proposal(factory)
    lock_acquired = asyncio.Event()
    release_cancel = asyncio.Event()
    real_transition = work_order_api.transition_work_order

    async def pause_after_order_lock(*args, **kwargs):
        lock_acquired.set()
        await release_cancel.wait()
        return await real_transition(*args, **kwargs)

    monkeypatch.setattr(work_order_api, "transition_work_order", pause_after_order_lock)

    async def cancel():
        async with factory() as db:
            return await work_order_api.cancel_order(run["work_order_id"], db, _DEV_USER)

    cancellation = asyncio.create_task(cancel())
    await asyncio.wait_for(lock_acquired.wait(), timeout=2)
    delivery = asyncio.create_task(deliver(factory, payload, key))
    await asyncio.sleep(0)
    release_cancel.set()
    await cancellation
    with pytest.raises(HTTPException, match="fence is no longer valid"):
        await delivery
    async with factory() as db:
        assert not await db.scalar(
            select(AgentTask.id).where(AgentTask.objective == payload.objective)
        )
        assert (await db.get(WorkOrder, run["work_order_id"])).status == "canceled"


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
        receipt = await db.scalar(
            select(ActionReceipt).where(ActionReceipt.logical_action_id == action_id)
        )
        receipt.response = {"id": "corrupt"}
        await db.commit()
    with pytest.raises(HTTPException, match="integrity mismatch"):
        await deliver(factory, payload, key)


@pytest.mark.asyncio
async def test_receipt_without_mandatory_provenance_is_not_reused(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, action_id, _, key = await proposal(factory)
    await deliver(factory, payload, key)
    async with factory() as db:
        receipt = await db.scalar(
            select(ActionReceipt).where(ActionReceipt.logical_action_id == action_id)
        )
        receipt.provenance = {}
        await db.commit()
    with pytest.raises(HTTPException, match="integrity mismatch"):
        await deliver(factory, payload, key)


@pytest.mark.asyncio
async def test_backward_read_normalizes_valid_pilot_event(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    _, run, action_id, _, key = await proposal(factory)
    artifact_id = str(uuid.uuid4())
    response = {"id": artifact_id, "updated_at": datetime.now(UTC).isoformat()}
    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        event_row = await append_event(
            db,
            run["work_order_id"],
            "chat.recipient_committed",
            actor="recipient:agent_control.task_propose",
            payload={
                "action_id": str(action_id),
                "attempt_id": key.split(":")[1],
                "operation": "agent_control.task_propose",
                "request_digest": action.request_digest,
                "response": response,
                "response_digest": digest(response),
                "evidence_scope": "database_commit",
                "can_replay": False,
            },
        )
        await db.commit()
        receipt = await read_receipt(db, action)
    assert receipt["logical_action_id"] == str(action_id)
    assert receipt["artifact_id"] == artifact_id
    assert receipt["artifact_revision"] == response["updated_at"]
    assert receipt["receipt_version"] == 1
    assert receipt["provenance"]["event_id"] == str(event_row.id)


@pytest.mark.asyncio
async def test_idempotency_key_is_not_bearer_authorization(client, test_engine):
    from app.main import app

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, _, _, key = await proposal(factory)
    viewer = _DEV_USER.model_copy(update={"roles": [UserRole.viewer]})
    app.dependency_overrides[get_current_user] = lambda: viewer
    response = await client.post(
        "/api/agent/tasks/propose",
        json=payload.model_dump(mode="json"),
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 403
    async with factory() as db:
        assert not await db.scalar(
            select(AgentTask.id).where(AgentTask.objective == payload.objective)
        )


@pytest.mark.asyncio
async def test_legacy_proposal_without_key_remains_available(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    result = await deliver(factory, AgentTaskPropose(objective="Legacy proposal"), None)
    assert result.status == "proposed"


@pytest.mark.asyncio
async def test_warehouse_keyed_asgi_response_is_exact_committed_receipt(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, action_id, item_id, key = await warehouse_update_proposal(factory)

    response = await deliver_warehouse_asgi(factory, item_id, payload, key)
    assert response.status_code == 200
    committed = response.json()

    async with factory() as db:
        receipt = await db.scalar(
            select(ActionReceipt).where(ActionReceipt.logical_action_id == action_id)
        )
        assert receipt is not None
        assert receipt.response == committed
        assert receipt.operation == "warehouse.update_item"
        assert (
            await db.scalar(
                select(func.count()).select_from(InventoryItem).where(InventoryItem.id == item_id)
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ActionReceipt)
                .where(ActionReceipt.logical_action_id == action_id)
            )
            == 1
        )

    # A new ASGI request sees exactly the timestamp frozen in the receipt, not
    # a pre-flush version that changed on commit.
    after = await deliver_warehouse_asgi(factory, item_id, {}, None)
    assert after.status_code == 200
    assert after.json()["updated_at"] == committed["updated_at"]


@pytest.mark.asyncio
async def test_warehouse_concurrent_lost_response_has_one_item_mutation_and_receipt(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, item_id, key = await warehouse_update_proposal(factory)
    first, second = await asyncio.gather(
        deliver_warehouse(factory, item_id, payload, key),
        deliver_warehouse(factory, item_id, payload, key),
    )
    assert first == second
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(InventoryItem).where(InventoryItem.id == item_id)
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ActionReceipt)
                .where(ActionReceipt.logical_action_id == action_id)
            )
            == 1
        )
        action = await db.get(ChatLogicalAction, action_id)
        assert action.result is None  # The worker lost its HTTP response/checkpoint.
        order = await db.get(WorkOrder, run["work_order_id"], with_for_update=True)
        order.status = "blocked"
        await db.commit()
    assert await deliver_warehouse(factory, item_id, payload, key) == first


@pytest.mark.asyncio
async def test_warehouse_receipt_failure_rolls_back_item_mutation(test_engine, monkeypatch):
    from app.domain import action_receipts

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, action_id, item_id, key = await warehouse_update_proposal(factory)
    real = action_receipts.record_warehouse_update_receipt

    async def fail_after_insert(*args):
        await real(*args)
        raise RuntimeError("Crash before warehouse commit")

    monkeypatch.setattr(action_receipts, "record_warehouse_update_receipt", fail_after_insert)
    with pytest.raises(RuntimeError, match="Crash before warehouse commit"):
        await deliver_warehouse(factory, item_id, payload, key)
    async with factory() as db:
        item = await db.get(InventoryItem, item_id)
        assert item.location != payload.get("location")
        assert not await db.scalar(
            select(ActionReceipt.id).where(ActionReceipt.logical_action_id == action_id)
        )


@pytest.mark.asyncio
async def test_warehouse_cancellation_and_recipient_share_order_lock(test_engine, monkeypatch):
    from app.api import work_orders as work_order_api

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, item_id, key = await warehouse_update_proposal(factory)
    lock_acquired = asyncio.Event()
    release_cancel = asyncio.Event()
    real_transition = work_order_api.transition_work_order

    async def pause_after_order_lock(*args, **kwargs):
        lock_acquired.set()
        await release_cancel.wait()
        return await real_transition(*args, **kwargs)

    monkeypatch.setattr(work_order_api, "transition_work_order", pause_after_order_lock)

    async def cancel():
        async with factory() as db:
            return await work_order_api.cancel_order(run["work_order_id"], db, _DEV_USER)

    cancellation = asyncio.create_task(cancel())
    await asyncio.wait_for(lock_acquired.wait(), timeout=2)
    delivery = asyncio.create_task(deliver_warehouse(factory, item_id, payload, key))
    await asyncio.sleep(0)
    release_cancel.set()
    await cancellation
    with pytest.raises(HTTPException, match="fence is no longer valid"):
        await delivery
    async with factory() as db:
        assert not await db.scalar(
            select(ActionReceipt.id).where(ActionReceipt.logical_action_id == action_id)
        )
        assert (await db.get(WorkOrder, run["work_order_id"])).status == "canceled"


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", ["item", "payload", "action", "owner", "attempt"])
async def test_warehouse_receipt_rejects_foreign_or_mismatched_delivery(test_engine, violation):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, action_id, item_id, key = await warehouse_update_proposal(factory)
    user = _DEV_USER
    delivered_item_id = item_id
    delivered_payload = payload
    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        if violation == "item":
            delivered_item_id = uuid.uuid4()
        elif violation == "payload":
            delivered_payload = {**payload, "location": "substituted"}
        elif violation == "action":
            args = json.loads(action.request["arguments"])
            action.request = {
                **action.request,
                "arguments": json.dumps({**args, "action": "create_item"}),
            }
            action.request_digest = digest(action.request)
        elif violation == "owner":
            user = _DEV_USER.model_copy(update={"sub": "foreign-owner"})
        else:
            key = f"{action_id}:{uuid.uuid4()}"
        await db.commit()
    with pytest.raises(HTTPException) as exc:
        await deliver_warehouse(factory, delivered_item_id, delivered_payload, key, user)
    assert exc.value.status_code in {404, 409}
    async with factory() as db:
        assert not await db.scalar(
            select(ActionReceipt.id).where(ActionReceipt.logical_action_id == action_id)
        )


@pytest.mark.asyncio
async def test_warehouse_key_is_not_bearer_and_legacy_authorized_update_remains_available(
    test_engine,
):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, action_id, item_id, key = await warehouse_update_proposal(factory)
    foreign = _DEV_USER.model_copy(update={"sub": "foreign-owner"})
    denied = await deliver_warehouse_asgi(factory, item_id, payload, key, foreign)
    assert denied.status_code == 404
    async with factory() as db:
        assert not await db.scalar(
            select(ActionReceipt.id).where(ActionReceipt.logical_action_id == action_id)
        )

    legacy = await deliver_warehouse_asgi(factory, item_id, payload, None)
    assert legacy.status_code == 200
    assert legacy.json()["location"] == payload["location"]
    async with factory() as db:
        assert not await db.scalar(
            select(ActionReceipt.id).where(ActionReceipt.logical_action_id == action_id)
        )


@pytest.mark.asyncio
async def test_warehouse_committed_receipt_allows_only_source_or_current_attempt(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, action_id, item_id, old_key = await warehouse_update_proposal(factory)
    committed = await deliver_warehouse(factory, item_id, payload, old_key)
    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        original_attempt = await db.get(WorkStepAttempt, action.attempt_id)
        next_attempt = WorkStepAttempt(
            step_id=original_attempt.step_id,
            attempt_no=original_attempt.attempt_no + 1,
            worker_id="warehouse-replacement-worker",
            status="running",
        )
        db.add(next_attempt)
        await db.flush()
        action.attempt_id = next_attempt.id
        await db.commit()
        current_key = f"{action_id}:{next_attempt.id}"

    assert await deliver_warehouse(factory, item_id, payload, old_key) == committed
    assert await deliver_warehouse(factory, item_id, payload, current_key) == committed
    with pytest.raises(HTTPException, match="attempt mismatch"):
        await deliver_warehouse(factory, item_id, payload, f"{action_id}:{uuid.uuid4()}")


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
async def test_durable_gateway_key_is_sent_only_to_exact_receipt_recipients(monkeypatch):
    """The journal key is transport metadata for the two E07/E08 recipients only."""
    from app.ai import agent_loop
    from app.ai.policy_engine import PolicyDecision

    session = agent_loop.AgentSession(AsyncMock())
    session._log_action = AsyncMock()
    session._skill_map = {
        "warehouse__update_item": {
            "name": "warehouse",
            "path": "/api/agent/cap/warehouse",
        },
        "agent_control__task_propose": {
            "name": "agent_control",
            "path": "/api/agent/cap/agent_control",
        },
        "warehouse__create_item": {
            "name": "warehouse",
            "path": "/api/agent/cap/warehouse",
        },
    }
    action_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    session._checkpoint_action_ids = {
        "warehouse-update": action_id,
        "task-propose": action_id,
        "warehouse-create": action_id,
    }
    session._recipient_attempt_id = attempt_id
    sent: list[tuple[str, str, str | None]] = []

    async def fake_execute(skill, args, _config, **kwargs):
        sent.append((skill["path"], args["action"], kwargs.get("idempotency_key")))
        return {"ok": True}

    monkeypatch.setattr(agent_loop, "execute_skill", fake_execute)
    monkeypatch.setattr(
        "app.ai.policy_engine.check_tool_execution",
        lambda **_: PolicyDecision(allowed=True),
    )

    for call_id, name, args in [
        ("warehouse-update", "warehouse__update_item", {"action": "update_item"}),
        ("task-propose", "agent_control__task_propose", {"action": "task_propose"}),
        ("warehouse-create", "warehouse__create_item", {"action": "create_item"}),
    ]:
        await session._execute_single_tool(
            {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}},
            1,
        )

    assert sent == [
        ("/api/agent/cap/warehouse", "update_item", f"{action_id}:{attempt_id}"),
        ("/api/agent/cap/agent_control", "task_propose", f"{action_id}:{attempt_id}"),
        ("/api/agent/cap/warehouse", "create_item", None),
    ]


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
async def test_verification_rejects_corrupt_receipt_bindings(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, _, key = await proposal(factory)
    await deliver(factory, payload, key)
    async with factory() as db:
        receipt = await db.scalar(
            select(ActionReceipt).where(ActionReceipt.logical_action_id == action_id)
        )
        receipt.response = {"id": "corrupt"}
        await db.commit()
        action = await db.get(ChatLogicalAction, action_id)
        with pytest.raises(HTTPException, match="integrity mismatch"):
            await verify_proposal_receipt(db, action, _DEV_USER)

    payload, run, action_id, _, key = await proposal(factory)
    await deliver(factory, payload, key)
    async with factory() as db:
        receipt = await db.scalar(
            select(ActionReceipt).where(ActionReceipt.logical_action_id == action_id)
        )
        receipt.operation = "other.recipient"
        await db.commit()
        action = await db.get(ChatLogicalAction, action_id)
        with pytest.raises(HTTPException, match="integrity mismatch"):
            await verify_proposal_receipt(db, action, _DEV_USER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("malformed_kind", "message"),
    [("invalid_uuid", "artifact binding is invalid"), ("missing_id", "integrity mismatch")],
)
async def test_verification_rejects_malformed_artifact_binding(
    test_engine, malformed_kind, message
):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, _, key = await proposal(factory)
    committed = await deliver(factory, payload, key)
    async with factory() as db:
        receipt = await db.scalar(
            select(ActionReceipt).where(ActionReceipt.logical_action_id == action_id)
        )
        original = receipt.response
        if malformed_kind == "invalid_uuid":
            malformed = {**original, "id": "not-a-uuid"}
        else:
            malformed = {name: value for name, value in original.items() if name != "id"}
        receipt.response = malformed
        receipt.response_digest = digest(malformed)
        receipt.artifact_id = malformed.get("id", "")
        await db.commit()
        action = await db.get(ChatLogicalAction, action_id)
        with pytest.raises(HTTPException, match=message):
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


@pytest.mark.asyncio
async def test_artifact_registry_verifies_only_reviewed_recipient_types(test_engine):
    assert supported_artifact_verifiers() == (
        ("agent_control.task_propose", "agent_task"),
        ("warehouse.update_item", "inventory_item"),
    )

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, action_id, item_id, key = await warehouse_update_proposal(factory)
    committed = await deliver_warehouse(factory, item_id, payload, key)
    async with factory() as db:
        verdict = await verify_action_artifact(
            db,
            action=await db.get(ChatLogicalAction, action_id),
            user=_DEV_USER,
        )

    assert verdict["status"] == "matched"
    assert verdict["operation"] == "warehouse.update_item"
    assert verdict["artifact_type"] == "inventory_item"
    assert verdict["artifact_id"] == str(item_id)
    assert verdict["artifact_version"] == committed["updated_at"]
    assert verdict["artifact_hash"] == verdict["expected_artifact_hash"]
    assert verdict["scope"] == "inventory_item_database_snapshot"
    assert verdict["evidence_source"] == "database:inventory_items"
    assert validate_artifact_verdict(verdict) is True
    assert verdict["can_resume"] is False


@pytest.mark.asyncio
async def test_unsupported_external_recipient_fails_closed_without_following_reference(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, action_id, _, key = await proposal(factory)
    await deliver(factory, payload, key)
    unsupported_request = {
        "name": "external_recipient",
        "arguments": {"action": "write", "reference": "https://untrusted.invalid/effect"},
    }
    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        receipt = await db.scalar(
            select(ActionReceipt).where(ActionReceipt.logical_action_id == action_id)
        )
        action.request = unsupported_request
        action.request_digest = digest(unsupported_request)
        receipt.operation = "external_recipient.write"
        receipt.request_digest = action.request_digest
        await db.commit()
    async with factory() as db:
        verdict = await verify_action_artifact(
            db,
            action=await db.get(ChatLogicalAction, action_id),
            user=_DEV_USER,
        )
    assert verdict["status"] == "inconclusive"
    assert verdict["reason"] == "unsupported_recipient_artifact"
    assert verdict["operation"] == "external_recipient.write"
    assert verdict["external_reference"] == "https://untrusted.invalid/effect"


@pytest.mark.asyncio
async def test_stale_artifact_and_forged_verdict_do_not_prove_current_state(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, action_id, _, key = await proposal(factory)
    committed = await deliver(factory, payload, key)
    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        original = await verify_action_artifact(db, action=action, user=_DEV_USER)
    assert original["status"] == "matched"

    forged = {**original, "artifact_hash": "0" * 64}
    assert validate_artifact_verdict(forged) is False

    async with factory() as db:
        task = await db.get(AgentTask, uuid.UUID(committed["id"]))
        task.status = "created"
        await db.commit()
    async with factory() as db:
        current = await verify_action_artifact(
            db,
            action=await db.get(ChatLogicalAction, action_id),
            user=_DEV_USER,
        )
    assert current["status"] == "changed"
    assert current["artifact_version"] != original["artifact_version"]
    assert current["artifact_hash"] != original["artifact_hash"]
    assert verdict_proves_current_artifact(original, current) is False


@pytest.mark.asyncio
async def test_unavailable_recipient_and_missing_descriptor_version_are_inconclusive(
    test_engine, monkeypatch
):
    from app.domain import artifact_verification

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, _, action_id, item_id, key = await warehouse_update_proposal(factory)
    await deliver_warehouse(factory, item_id, payload, key)

    async def unavailable(*_args, **_kwargs):
        raise SQLAlchemyError("recipient unavailable")

    monkeypatch.setattr(artifact_verification, "_recipient_snapshot", unavailable)
    async with factory() as db:
        unavailable_verdict = await verify_action_artifact(
            db,
            action=await db.get(ChatLogicalAction, action_id),
            user=_DEV_USER,
        )
    assert unavailable_verdict["status"] == "inconclusive"
    assert unavailable_verdict["reason"] == "recipient_unavailable"

    monkeypatch.undo()
    async with factory() as db:
        descriptor = await db.scalar(
            select(WorkArtifact).where(
                WorkArtifact.metadata_["logical_action_id"].as_string() == str(action_id)
            )
        )
        metadata = dict(descriptor.metadata_)
        metadata.pop("descriptor_version")
        descriptor.metadata_ = metadata
        await db.commit()
    async with factory() as db:
        missing_version = await verify_action_artifact(
            db,
            action=await db.get(ChatLogicalAction, action_id),
            user=_DEV_USER,
        )
    assert missing_version["status"] == "inconclusive"
    assert missing_version["reason"] == "artifact_descriptor_version_missing"


@pytest.mark.asyncio
async def test_external_reference_is_opaque_text_and_match_does_not_complete_other_criteria(
    test_engine,
):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    payload, run, action_id, _, key = await proposal(factory)
    await deliver(factory, payload, key)
    reference = "https://untrusted.invalid/do-not-fetch"
    async with factory() as db:
        descriptor = await db.scalar(
            select(WorkArtifact).where(
                WorkArtifact.metadata_["logical_action_id"].as_string() == str(action_id)
            )
        )
        descriptor.uri = reference
        criterion = WorkAcceptanceCriterion(
            work_order_id=run["work_order_id"],
            criterion_key="other_required_result",
            description="A separate result is still required",
            kind="semantic",
            required=True,
            predicate={},
        )
        db.add(criterion)
        await db.commit()
        before_evidence = await db.scalar(
            select(func.count())
            .select_from(WorkEvidence)
            .where(WorkEvidence.work_order_id == run["work_order_id"])
        )

    async with factory() as db:
        verdict = await verify_action_artifact(
            db,
            action=await db.get(ChatLogicalAction, action_id),
            user=_DEV_USER,
        )
        order = await db.get(WorkOrder, run["work_order_id"])
        criterion = await db.scalar(
            select(WorkAcceptanceCriterion).where(
                WorkAcceptanceCriterion.work_order_id == order.id,
                WorkAcceptanceCriterion.criterion_key == "other_required_result",
            )
        )
        after_evidence = await db.scalar(
            select(func.count())
            .select_from(WorkEvidence)
            .where(WorkEvidence.work_order_id == order.id)
        )

    assert verdict["status"] == "matched"
    assert verdict["external_reference"] == reference
    assert order.status != "completed"
    assert criterion.status == "pending"
    assert criterion.verdict is None
    assert after_evidence == before_evidence


@pytest.mark.asyncio
async def test_artifact_descriptor_migration_backfills_supported_receipts_only(test_engine):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    path = (
        Path(__file__).parents[1]
        / "migrations/versions/20260922_0002_artifact_verification_descriptors.py"
    )
    spec = importlib.util.spec_from_file_location("artifact_descriptor_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "artifact_descriptor_" + uuid.uuid4().hex
    order_id, step_id, attempt_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    receipts = [
        (uuid.uuid4(), "agent_control.task_propose"),
        (uuid.uuid4(), "warehouse.update_item"),
        (uuid.uuid4(), "unreviewed.external_write"),
    ]

    async with test_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await conn.execute(text("CREATE TABLE work_orders (id UUID PRIMARY KEY)"))
        await conn.execute(
            text(
                "CREATE TABLE work_steps (id UUID PRIMARY KEY, "
                "work_order_id UUID NOT NULL REFERENCES work_orders(id))"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE work_step_attempts (id UUID PRIMARY KEY, "
                "step_id UUID NOT NULL REFERENCES work_steps(id))"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE action_receipts (logical_action_id UUID PRIMARY KEY, "
                "work_order_id UUID NOT NULL REFERENCES work_orders(id), attempt_id UUID NOT NULL "
                "REFERENCES work_step_attempts(id), operation VARCHAR(200) NOT NULL, "
                "artifact_id VARCHAR(300) NOT NULL, artifact_revision VARCHAR(300) NOT NULL, "
                "response_digest VARCHAR(64) NOT NULL, receipt_version SMALLINT NOT NULL, "
                "created_at TIMESTAMPTZ NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE work_artifacts (id UUID PRIMARY KEY, work_order_id UUID NOT NULL, "
                "step_id UUID, artifact_type VARCHAR(50) NOT NULL, name VARCHAR(500) NOT NULL, "
                "uri TEXT, content_hash VARCHAR(128), content_type VARCHAR(150), size_bytes INT, "
                "metadata JSON NOT NULL, created_at TIMESTAMPTZ NOT NULL, "
                "updated_at TIMESTAMPTZ NOT NULL)"
            )
        )
        await conn.execute(text("INSERT INTO work_orders (id) VALUES (:id)"), {"id": order_id})
        await conn.execute(
            text("INSERT INTO work_steps (id, work_order_id) VALUES (:id, :order_id)"),
            {"id": step_id, "order_id": order_id},
        )
        await conn.execute(
            text("INSERT INTO work_step_attempts (id, step_id) VALUES (:id, :step_id)"),
            {"id": attempt_id, "step_id": step_id},
        )
        for action_id, operation in receipts:
            await conn.execute(
                text(
                    "INSERT INTO action_receipts "
                    "(logical_action_id, work_order_id, attempt_id, operation, artifact_id, "
                    "artifact_revision, response_digest, receipt_version, created_at) VALUES "
                    "(:action_id, :order_id, :attempt_id, :operation, :artifact_id, "
                    ":revision, :hash, 1, now())"
                ),
                {
                    "action_id": action_id,
                    "order_id": order_id,
                    "attempt_id": attempt_id,
                    "operation": operation,
                    "artifact_id": str(uuid.uuid4()),
                    "revision": datetime.now(UTC).isoformat(),
                    "hash": uuid.uuid4().hex * 2,
                },
            )

        def upgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.upgrade()

        def downgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.downgrade()

        await conn.run_sync(upgrade)
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT artifact_type, step_id, content_hash, metadata "
                        "FROM work_artifacts ORDER BY artifact_type"
                    )
                )
            )
            .mappings()
            .all()
        )
        assert [row.artifact_type for row in rows] == ["agent_task", "inventory_item"]
        assert all(row.step_id == step_id for row in rows)
        assert all(len(row.content_hash) == 64 for row in rows)
        assert all(row.metadata["descriptor_version"] == 1 for row in rows)
        assert all(row.metadata["migration"] == "20260922_0002" for row in rows)

        await conn.run_sync(downgrade)
        assert await conn.scalar(text("SELECT count(*) FROM work_artifacts")) == 0
        await conn.rollback()


@pytest.mark.asyncio
async def test_scalable_receipt_migration_roundtrip_quarantines_corrupt_and_duplicate(
    test_engine,
):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    migration_path = (
        Path(__file__).parents[1] / "migrations/versions/20260913_0001_scalable_action_receipts.py"
    )
    spec = importlib.util.spec_from_file_location("scalable_receipt_migration", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "receipt_test_" + uuid.uuid4().hex
    owner = "migration-owner"
    order_id = uuid.uuid4()
    step_id = uuid.uuid4()
    cases = {
        "valid": (uuid.uuid4(), uuid.uuid4()),
        "corrupt": (uuid.uuid4(), uuid.uuid4()),
        "duplicate": (uuid.uuid4(), uuid.uuid4()),
        "foreign_attempt": (uuid.uuid4(), uuid.uuid4()),
    }

    async with test_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await conn.execute(
            text("CREATE TABLE work_orders (id UUID PRIMARY KEY, owner_key VARCHAR(200) NOT NULL)")
        )
        await conn.execute(
            text(
                "CREATE TABLE work_steps (id UUID PRIMARY KEY, "
                "work_order_id UUID NOT NULL REFERENCES work_orders(id))"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE work_step_attempts (id UUID PRIMARY KEY, "
                "step_id UUID NOT NULL REFERENCES work_steps(id))"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE chat_logical_actions (id UUID PRIMARY KEY, "
                "work_order_id UUID NOT NULL REFERENCES work_orders(id), "
                "attempt_id UUID NOT NULL REFERENCES work_step_attempts(id), "
                "request JSON NOT NULL, request_digest VARCHAR(64) NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE work_events (id UUID PRIMARY KEY, work_order_id UUID NOT NULL "
                "REFERENCES work_orders(id), sequence INTEGER NOT NULL, event_type VARCHAR(100) "
                "NOT NULL, actor VARCHAR(200) NOT NULL, payload JSON NOT NULL, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        await conn.execute(
            text("INSERT INTO work_orders (id, owner_key) VALUES (:id, :owner)"),
            {"id": order_id, "owner": owner},
        )
        await conn.execute(
            text("INSERT INTO work_steps (id, work_order_id) VALUES (:id, :order_id)"),
            {"id": step_id, "order_id": order_id},
        )

        sequence = 0
        for kind, (action_id, attempt_id) in cases.items():
            request = {
                "name": "agent_control",
                "arguments": json.dumps({"action": "task_propose", "body": {"objective": kind}}),
            }
            request_digest = digest(request)
            response = {
                "id": str(uuid.uuid4()),
                "updated_at": datetime.now(UTC).isoformat(),
            }
            payload = {
                "action_id": str(action_id),
                "attempt_id": str(attempt_id),
                "operation": "agent_control.task_propose",
                "request_digest": request_digest,
                "response": response,
                "response_digest": digest(response),
                "evidence_scope": "database_commit",
                "can_replay": False,
            }
            if kind == "corrupt":
                payload["response_digest"] = "0" * 64
            await conn.execute(
                text("INSERT INTO work_step_attempts (id, step_id) VALUES (:id, :step_id)"),
                {"id": attempt_id, "step_id": step_id},
            )
            action_attempt_id = attempt_id
            if kind == "foreign_attempt":
                action_attempt_id = uuid.uuid4()
                await conn.execute(
                    text("INSERT INTO work_step_attempts (id, step_id) VALUES (:id, :step_id)"),
                    {"id": action_attempt_id, "step_id": step_id},
                )
            await conn.execute(
                text(
                    "INSERT INTO chat_logical_actions "
                    "(id, work_order_id, attempt_id, request, request_digest) "
                    "VALUES (:id, :order_id, :attempt_id, CAST(:request AS JSON), :request_digest)"
                ),
                {
                    "id": action_id,
                    "order_id": order_id,
                    "attempt_id": action_attempt_id,
                    "request": json.dumps(request),
                    "request_digest": request_digest,
                },
            )
            repeats = 2 if kind == "duplicate" else 1
            for _ in range(repeats):
                sequence += 1
                await conn.execute(
                    text(
                        "INSERT INTO work_events "
                        "(id, work_order_id, sequence, event_type, actor, payload) VALUES "
                        "(:id, :order_id, :sequence, 'chat.recipient_committed', "
                        "'recipient:test', CAST(:payload AS JSON))"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "order_id": order_id,
                        "sequence": sequence,
                        "payload": json.dumps(payload),
                    },
                )

        def upgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.upgrade()

        def downgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.downgrade()

        await conn.run_sync(upgrade)
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT logical_action_id, owner_key, artifact_id, artifact_revision, "
                        "receipt_version, provenance FROM action_receipts"
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].logical_action_id == cases["valid"][0]
        assert rows[0].owner_key == owner
        assert rows[0].receipt_version == 1
        assert rows[0].provenance["migration"] == "20260913_0001"
        assert (
            await conn.scalar(text("SELECT count(*) FROM work_events")) == 5
        )  # quarantine source rows are retained

        await conn.run_sync(downgrade)

        def assert_dropped(sync):
            assert "action_receipts" not in inspect(sync).get_table_names()

        await conn.run_sync(assert_dropped)
        await conn.run_sync(upgrade)
        assert await conn.scalar(text("SELECT count(*) FROM action_receipts")) == 1
        await conn.rollback()
