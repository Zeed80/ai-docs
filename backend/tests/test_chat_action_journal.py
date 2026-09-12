"""Logical identity, atomic persistence and non-authorizing human observations."""

import copy
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.chat_runs import (
    ActionObservationRequest,
    ChatRunCreate,
    get_chat_action,
    get_chat_actions,
    observe_chat_action,
    submit_chat_run,
)
from app.auth.jwt import _DEV_USER
from app.db.agent_runtime_models import ChatLogicalAction
from app.db.models import WorkEvent, WorkOrder, WorkStep, WorkStepAttempt
from app.domain.chat_action_journal import action_state, record_boundary
from app.domain.work_orders import claim_ready_step, fail_attempt


async def setup_action(factory):
    async with factory() as db:
        run = await submit_chat_run(
            ChatRunCreate(request_id=uuid.uuid4(), content="Journal test"), db, _DEV_USER
        )
    async with factory() as db:
        order, step, attempt = await claim_ready_step(
            db, worker_id="journal-worker", work_order_id=run["work_order_id"]
        )
        call = {"id": "call-1", "function": {"name": "test", "arguments": '{"id":1}'}}
        action_id = uuid.uuid4()
        payload = {
            "phase": "tools_planned",
            "pending_calls": [call],
            "action_ids": {"call-1": str(action_id)},
            "in_flight_call_id": None,
        }
        await record_boundary(db, order, attempt, payload)
        await db.commit()
        return run, step.id, attempt.id, action_id, payload


async def boundary(factory, run, attempt_id, payload):
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"], with_for_update=True)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        await record_boundary(db, order, attempt, payload)
        attempt.checkpoint = {"test_boundary": payload["phase"]}
        await db.commit()


@pytest.mark.asyncio
async def test_journal_result_and_checkpoint_rollback_together(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, _, attempt_id, action_id, payload = await setup_action(factory)
    started = {**payload, "phase": "tool_started", "in_flight_call_id": "call-1"}
    await boundary(factory, run, attempt_id, started)
    recorded = {
        **payload,
        "phase": "tool_recorded",
        "pending_calls": [],
        "completed_call": {
            "action_id": str(action_id),
            "call_id": "call-1",
            "result": {"error": "timeout", "status": "outcome_unknown"},
        },
    }
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"], with_for_update=True)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        await record_boundary(db, order, attempt, recorded)
        attempt.checkpoint = {"test_boundary": "tool_recorded"}
        await db.flush()
        await db.rollback()
    async with factory() as db:
        assert (await db.get(ChatLogicalAction, action_id)).status == "started"
        assert (await db.get(WorkStepAttempt, attempt_id)).checkpoint == {
            "test_boundary": "tool_started"
        }
        assert not await db.scalar(
            select(WorkEvent.id).where(
                WorkEvent.work_order_id == run["work_order_id"],
                WorkEvent.event_type == "chat.action_result_recorded",
            )
        )
    await boundary(factory, run, attempt_id, recorded)
    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        assert action.status == "outcome_unknown"  # Honor the tool's explicit structured status.
        assert action.result == recorded["completed_call"]["result"]
    with pytest.raises(ValueError, match="cannot execute again"):
        await boundary(factory, run, attempt_id, payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["arguments", "call_id", "foreign_order", "duplicate_id"])
async def test_identity_rebinding_is_rejected(test_engine, change):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, _, attempt_id, action_id, payload = await setup_action(factory)
    changed = copy.deepcopy(payload)
    if change == "arguments":
        changed["pending_calls"][0]["function"]["arguments"] = '{"id":2}'
    elif change == "call_id":
        changed["pending_calls"][0]["id"] = "different"
        changed["action_ids"] = {"different": str(action_id)}
    elif change == "foreign_order":
        run, _, attempt_id, _, _ = await setup_action(factory)
    else:
        changed["pending_calls"].append({"id": "other", "function": {"name": "test"}})
        changed["action_ids"]["other"] = str(action_id)
    with pytest.raises(ValueError):
        await boundary(factory, run, attempt_id, changed)


@pytest.mark.asyncio
async def test_same_model_call_id_in_new_batch_is_a_distinct_action(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, _, attempt_id, first_id, payload = await setup_action(factory)
    second_id = uuid.uuid4()
    await boundary(factory, run, attempt_id, {**payload, "action_ids": {"call-1": str(second_id)}})
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatLogicalAction)
                .where(ChatLogicalAction.work_order_id == run["work_order_id"])
            )
            == 2
        )
        assert (await db.get(ChatLogicalAction, first_id)).call_id == (
            await db.get(ChatLogicalAction, second_id)
        ).call_id


@pytest.mark.asyncio
async def test_waiting_confirmation_keeps_logical_id_across_attempts(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, attempt_id, action_id, payload = await setup_action(factory)
    await boundary(
        factory,
        run,
        attempt_id,
        {**payload, "phase": "tool_started", "in_flight_call_id": "call-1"},
    )
    await boundary(
        factory,
        run,
        attempt_id,
        {**payload, "phase": "confirmation_required", "in_flight_call_id": "call-1"},
    )
    async with factory() as db:
        # The continuation route creates and authorizes the real next step;
        # this fixture isolates the journal's identity across attempts.
        next_attempt = WorkStepAttempt(
            step_id=step_id, attempt_no=2, worker_id="next", status="running"
        )
        db.add(next_attempt)
        await db.commit()
    await boundary(factory, run, next_attempt.id, payload)
    await boundary(
        factory,
        run,
        next_attempt.id,
        {**payload, "phase": "tool_started", "in_flight_call_id": "call-1"},
    )
    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        assert action.attempt_id == next_attempt.id
        assert action.status == "started"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatLogicalAction)
                .where(ChatLogicalAction.work_order_id == run["work_order_id"])
            )
            == 1
        )


@pytest.mark.asyncio
async def test_expired_lease_is_unknown_without_waiting_for_reaper(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, attempt_id, action_id, payload = await setup_action(factory)
    await boundary(
        factory,
        run,
        attempt_id,
        {**payload, "phase": "tool_started", "in_flight_call_id": "call-1"},
    )
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        action = await db.get(ChatLogicalAction, action_id)
        assert await action_state(db, order, action) == "started"
        step = await db.get(WorkStep, step_id)
        step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.flush()
        assert await action_state(db, order, action) == "outcome_unknown"
    with pytest.raises(ValueError, match="cannot be replayed"):
        await boundary(factory, run, attempt_id, payload)


@pytest.mark.asyncio
async def test_observations_are_owned_idempotent_and_never_authorize_replay(test_engine):
    import asyncio

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, attempt_id, action_id, payload = await setup_action(factory)
    await boundary(
        factory,
        run,
        attempt_id,
        {**payload, "phase": "tool_started", "in_flight_call_id": "call-1"},
    )
    async with factory() as db:
        action = await db.get(ChatLogicalAction, action_id)
        body = ActionObservationRequest(
            request_id=uuid.uuid4(),
            request_digest=action.request_digest,
            outcome="not_observed",
            note="No matching record in recipient log",
            evidence_reference="manual-log:2026-09-12",
        )
        with pytest.raises(HTTPException) as running:
            await observe_chat_action(run["id"], action_id, body, db, _DEV_USER)
        assert running.value.status_code == 409
        await fail_attempt(
            db,
            order=await db.get(WorkOrder, run["work_order_id"]),
            step=await db.get(WorkStep, step_id),
            attempt=await db.get(WorkStepAttempt, attempt_id),
            error={"code": "worker_lost"},
            retryable=False,
            actor="test",
        )
        await db.commit()

    async def observe():
        async with factory() as db:
            return await observe_chat_action(run["id"], action_id, body, db, _DEV_USER)

    first, second = await asyncio.gather(observe(), observe())
    assert first == second
    assert first["verified"] is False and first["can_replay"] is False
    async with factory() as db:
        page = await get_chat_actions(run["id"], 0, 100, db, _DEV_USER)
        assert page["items"][0]["status"] == "outcome_unknown"
        assert page["items"][0]["latest_observation"]["actor"] == _DEV_USER.sub
        assert (await db.get(WorkOrder, run["work_order_id"])).status == "blocked"
        assert (await db.get(ChatLogicalAction, action_id)).status == "started"
        for user, code in [
            (_DEV_USER.model_copy(update={"sub": "foreign"}), 404),
            (_DEV_USER.model_copy(update={"via_agent": True}), 403),
        ]:
            with pytest.raises(HTTPException) as denied:
                await observe_chat_action(run["id"], action_id, body, db, user)
            assert denied.value.status_code == code
        with pytest.raises(HTTPException) as conflict:
            await observe_chat_action(
                run["id"], action_id, body.model_copy(update={"outcome": "observed"}), db, _DEV_USER
            )
        assert conflict.value.status_code == 409
        with pytest.raises(HTTPException) as foreign:
            await get_chat_actions(
                run["id"], 0, 100, db, _DEV_USER.model_copy(update={"sub": "foreign"})
            )
        assert foreign.value.status_code == 404


@pytest.mark.asyncio
async def test_action_detail_checks_owner_binding_and_integrity(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, _, _, action_id, payload = await setup_action(factory)
    other, _, _, _, _ = await setup_action(factory)
    async with factory() as db:
        detail = await get_chat_action(run["id"], action_id, db, _DEV_USER)
        assert detail["request"] == payload["pending_calls"][0]["function"]
        assert detail["can_replay"] is False
        with pytest.raises(HTTPException) as wrong_run:
            await get_chat_action(other["id"], action_id, db, _DEV_USER)
        assert wrong_run.value.status_code == 404
        action = await db.get(ChatLogicalAction, action_id)
        action.request = {"name": "changed"}
        await db.flush()
        with pytest.raises(HTTPException) as tampered:
            await get_chat_action(run["id"], action_id, db, _DEV_USER)
        assert tampered.value.status_code == 409


@pytest.mark.asyncio
async def test_incremental_journal_migration_in_isolated_schema(test_engine):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    migration_path = (
        Path(__file__).parents[1] / "migrations/versions/20260912_0001_chat_action_journal.py"
    )
    spec = importlib.util.spec_from_file_location("chat_journal_migration", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "journal_test_" + uuid.uuid4().hex
    async with test_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await conn.execute(text("CREATE TABLE work_orders (id UUID PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE work_step_attempts (id UUID PRIMARY KEY)"))

        def migrate(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.upgrade()
                columns = {c["name"]: c for c in inspect(sync).get_columns("chat_logical_actions")}
                assert columns["request_digest"]["nullable"] is False
                assert columns["result_digest"]["nullable"] is True
                assert len(inspect(sync).get_foreign_keys("chat_logical_actions")) == 2
                module.downgrade()
                assert "chat_logical_actions" not in inspect(sync).get_table_names()
                module.upgrade()

        await conn.run_sync(migrate)
        await conn.rollback()
