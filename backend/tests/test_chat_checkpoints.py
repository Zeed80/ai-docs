"""Checkpoint integrity and tool-boundary persistence without automatic replay."""

import copy
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.agent_config import BuiltinAgentConfig
from app.ai.agent_loop import AgentSession
from app.ai.chat_checkpoint import ChatCheckpointError, pack_checkpoint, unpack_checkpoint
from app.api.chat_runs import (
    ChatResumeRequest,
    ChatRunCreate,
    get_chat_checkpoint,
    resume_chat_run,
    submit_chat_run,
)
from app.auth.jwt import _DEV_USER
from app.db.agent_runtime_models import ChatLogicalAction
from app.db.models import WorkEvent, WorkOrder, WorkPlan, WorkStep, WorkStepAttempt
from app.domain.work_orders import claim_ready_step, fail_attempt


def call(call_id="one"):
    return {"id": call_id, "function": {"name": "test", "arguments": {"id": call_id}}}


def state(**changes):
    return {
        "phase": "tools_planned",
        "messages": [{"role": "user", "content": "private"}],
        "pending_calls": [call()],
        "in_flight_call_id": None,
        **changes,
    }


def session(send=None):
    obj = AgentSession.__new__(AgentSession)
    obj._send = send or AsyncMock()
    obj._checkpoint_sink = None
    obj._checkpoint_pending = []
    obj._checkpoint_in_flight = None
    obj._granted_approvals = set()
    obj._iteration = 0
    obj.total_tokens = {"input_tokens": 0, "output_tokens": 0}
    obj._role_context = "role"
    obj._active_role = "worker"
    obj._turn_model_override = None
    obj._response_budget = 2048
    obj._excluded_tools = set()
    obj._recommended_capabilities = set()
    obj._workspace_expected = False
    obj._config = BuiltinAgentConfig()
    obj._effective_system = lambda: "Private system context"
    obj.messages = []
    obj._trim_history = lambda: None
    return obj


def test_snapshot_is_detached_and_tampering_is_rejected():
    source = state()
    snapshot = pack_checkpoint(source)
    source["messages"][0]["content"] = "changed"
    assert unpack_checkpoint(snapshot)["messages"][0]["content"] == "private"
    changed = copy.deepcopy(snapshot)
    changed["payload"]["pending_calls"][0]["function"]["arguments"]["id"] = "forged"
    with pytest.raises(ValueError, match="integrity"):
        unpack_checkpoint(changed)
    assert unpack_checkpoint(snapshot)["can_resume"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"pending_calls": [call("")]},
        {"pending_calls": [call(), call()]},
        {"in_flight_call_id": "foreign"},
        {"phase": "unknown"},
        {"messages": [{"content": "x" * 4_000_001}]},
    ],
)
def test_invalid_snapshots_are_rejected(changes):
    with pytest.raises(ValueError):
        pack_checkpoint(state(**changes))


@pytest.mark.asyncio
async def test_sequential_frontier_is_saved_before_and_after_each_tool():
    obj = session()
    snapshots = []

    async def persist(snapshot):
        snapshots.append(unpack_checkpoint(snapshot))

    async def execute(tc, iteration):
        assert snapshots[-1]["phase"] == "tool_started"
        assert snapshots[-1]["in_flight_call_id"] == tc["id"]
        return "test", {"answer": tc["id"]}, tc["id"]

    obj.set_checkpoint_sink(persist)
    obj._execute_single_tool = execute
    calls = [call(), call("two")]
    obj.messages = [{"role": "assistant", "tool_calls": calls}]
    # Durable parallel entrypoint deliberately uses the same sequential frontier.
    await obj._execute_tools_parallel(calls, 0)
    assert [s["phase"] for s in snapshots] == [
        "tools_planned",
        "tool_started",
        "tool_recorded",
        "tool_started",
        "tool_recorded",
    ]
    assert [c["id"] for c in snapshots[2]["pending_calls"]] == ["two"]
    assert snapshots[2]["messages"][-1]["tool_call_id"] == "one"
    assert snapshots[-1]["pending_calls"] == []
    assert snapshots[0]["action_ids"] == snapshots[-1]["action_ids"]
    assert snapshots[-1]["completed_call"]["action_id"] == snapshots[0]["action_ids"]["two"]
    assert set(obj.messages[0]["tool_calls"][0]) == {"id", "function"}


@pytest.mark.asyncio
async def test_failed_checkpoint_prevents_tool_execution():
    obj = session()
    obj.set_checkpoint_sink(AsyncMock(side_effect=ConnectionError("unavailable")))
    effect = AsyncMock()
    obj._execute_single_tool = effect
    with pytest.raises(ChatCheckpointError):
        await obj._execute_tools_sequential([call()], 0)
    effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_recording_result_does_not_execute_remaining_calls():
    obj = session()
    saved, effects = [], []

    async def persist(snapshot):
        payload = unpack_checkpoint(snapshot)
        if payload["phase"] == "tool_recorded":
            raise ConnectionError("result could not be persisted")
        saved.append(payload)

    async def execute(tc, iteration):
        effects.append(tc["id"])
        return "test", {"result": "already happened"}, tc["id"]

    obj.set_checkpoint_sink(persist)
    obj._execute_single_tool = execute
    with pytest.raises(ChatCheckpointError):
        await obj._execute_tools_sequential([call(), call("two")], 0)
    assert effects == ["one"]
    assert saved[-1]["in_flight_call_id"] == "one"
    assert saved[-1]["can_resume"] is False


async def claim(factory):
    async with factory() as db:
        run = await submit_chat_run(
            ChatRunCreate(request_id=uuid.uuid4(), content="Perform two actions"), db, _DEV_USER
        )
    async with factory() as db:
        _, step, attempt = await claim_ready_step(
            db, worker_id="checkpoint-worker", work_order_id=run["work_order_id"]
        )
        await db.commit()
        return run, step.id, attempt.id


@pytest.mark.asyncio
async def test_attempt_from_another_work_order_cannot_write_checkpoint(test_engine):
    from app.tasks.durable_chat import run_durable_chat

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    first, _, _ = await claim(factory)
    _, second_step, second_attempt = await claim(factory)
    with pytest.raises(RuntimeError, match="Execution stopped"):
        await run_durable_chat(
            first["work_order_id"], second_step, second_attempt, session_factory=factory
        )
    async with factory() as db:
        assert (await db.get(WorkStepAttempt, second_attempt)).checkpoint is None


@pytest.mark.asyncio
async def test_confirmation_keeps_prior_results_and_survives_failure_settlement(
    test_engine, monkeypatch
):
    from app.ai import orchestrator
    from app.tasks.work_orders import execute_claimed_step

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, attempt_id = await claim(factory)
    effects = []

    class Agent:
        def __init__(self, send):
            self._executor = session(send)

        def hydrate_history(self, history):
            self._executor.messages = list(history)

        async def on_user_message(self, prompt, **kwargs):
            calls = [call(), call("two")]
            self._executor.messages += [
                {"role": "user", "content": prompt},
                {"role": "assistant", "tool_calls": calls},
            ]

            async def execute(tc, iteration):
                if tc["id"] == "two":
                    await self._executor._request_approval("test", {"id": "two"})
                effects.append(tc["id"])
                return "test", {"result": tc["id"]}, tc["id"]

            self._executor._execute_single_tool = execute
            await self._executor._execute_tools_sequential(calls, 0)

    monkeypatch.setattr(orchestrator, "AgentOrchestrator", Agent)
    assert not await execute_claimed_step(step_id, attempt_id, session_factory=factory)
    assert effects == ["one"]
    async with factory() as db:
        attempt = await db.get(WorkStepAttempt, attempt_id)
        assert attempt.status == "failed"
        assert (await db.get(WorkOrder, run["work_order_id"])).status == "blocked"
        payload = unpack_checkpoint(attempt.checkpoint["snapshot"])
        assert payload["phase"] == "confirmation_required"
        assert payload["confirmation"] == {"tool": "test", "args": {"id": "two"}}
        assert [c["id"] for c in payload["pending_calls"]] == ["two"]
        assert payload["messages"][-1]["tool_call_id"] == "one"
        actions = list(
            await db.scalars(
                select(ChatLogicalAction).where(
                    ChatLogicalAction.work_order_id == run["work_order_id"]
                )
            )
        )
        assert {a.call_id: a.status for a in actions} == {
            "one": "result_recorded",
            "two": "waiting_confirmation",
        }
        assert next(a.result for a in actions if a.call_id == "one") == {"result": "one"}
        summary = await get_chat_checkpoint(run["id"], db, _DEV_USER)
        assert summary["available"] is True
        assert summary["can_resume"] is False
        assert "messages" not in summary
        with pytest.raises(HTTPException) as denied:
            await get_chat_checkpoint(
                run["id"], db, _DEV_USER.model_copy(update={"sub": "foreign"})
            )
        assert denied.value.status_code == 404
        order = await db.get(WorkOrder, run["work_order_id"])
        order.plan_revision += 1
        await db.flush()
        with pytest.raises(HTTPException) as stale:
            await get_chat_checkpoint(run["id"], db, _DEV_USER)
        assert stale.value.status_code == 409


async def stopped_confirmation(factory, monkeypatch, *, repeat=False):
    from app.ai import agent_config

    config = BuiltinAgentConfig()
    monkeypatch.setattr(agent_config, "get_builtin_agent_config", lambda: config)
    run, step_id, attempt_id = await claim(factory)
    obj = session()
    obj.messages = [
        {"role": "user", "content": "Perform two actions"},
        {"role": "assistant", "tool_calls": [call(), call("two")]},
        {"role": "tool", "tool_call_id": "one", "content": '{"result":"one"}'},
    ]
    obj._checkpoint_pending = [call("two")]
    if repeat:
        repeated = {"id": "three", "function": {"name": "test", "arguments": {"id": "two"}}}
        obj._checkpoint_pending.append(repeated)
        obj.messages[1]["tool_calls"].append(repeated)
    obj._checkpoint_in_flight = "two"
    sink = AsyncMock()
    obj.set_checkpoint_sink(sink)
    await obj.save_checkpoint("confirmation_required", {"tool": "test", "args": {"id": "two"}})
    snapshot = sink.call_args.args[0]
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        step = await db.get(WorkStep, step_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        await fail_attempt(
            db,
            order=order,
            step=step,
            attempt=attempt,
            error={"code": "confirmation_required"},
            retryable=False,
            actor="test",
            checkpoint={
                "kind": "durable_chat",
                "owner_key": order.owner_key,
                "work_order_id": str(order.id),
                "step_id": str(step.id),
                "attempt_id": str(attempt.id),
                "plan_id": str(step.plan_id),
                "plan_revision": order.plan_revision,
                "snapshot": snapshot,
            },
        )
        await db.commit()
    body = ChatResumeRequest(attempt_id=attempt_id, sha256=snapshot["sha256"], approved=True)
    return run, body


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", [False, True])
async def test_confirmation_resume_is_atomic_and_executes_only_pending_tail(
    test_engine, monkeypatch, repeat
):
    import asyncio

    from app.ai import orchestrator
    from app.tasks.work_orders import execute_claimed_step

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, body = await stopped_confirmation(factory, monkeypatch, repeat=repeat)
    async with factory() as db:
        summary = await get_chat_checkpoint(run["id"], db, _DEV_USER)
        assert summary["can_resume"] is True
        assert summary["confirmation"] == {"tool": "test", "args": {"id": "two"}}
        assert "messages" not in summary

    async def approve():
        async with factory() as db:
            return await resume_chat_run(run["id"], body, db, _DEV_USER)

    results = await asyncio.gather(approve(), approve())
    assert all(result["status"] == "ready" for result in results)
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkPlan)
                .where(WorkPlan.work_order_id == run["work_order_id"])
            )
            == 2
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkEvent)
                .where(
                    WorkEvent.work_order_id == run["work_order_id"],
                    WorkEvent.event_type == "chat.continuation_decided",
                )
            )
            == 1
        )
        _, step, attempt = await claim_ready_step(
            db, worker_id="resume-worker", work_order_id=run["work_order_id"]
        )
        await db.commit()
        step_id, attempt_id = step.id, attempt.id

    effects = []

    class Agent:
        def __init__(self, send):
            obj = self._executor = session(send)
            obj._refresh_runtime_config = lambda: None
            obj._init_mcp = AsyncMock()

            async def execute(tc, iteration):
                assert await obj._request_approval("test", tc["function"]["arguments"]) is True
                obj._granted_approvals.add("test")
                effects.append(tc["id"])
                return "test", {"result": tc["id"]}, tc["id"]

            async def finish(**kwargs):
                assert kwargs == {"start_iteration": 1, "restored": True}
                assert obj._granted_approvals == set()
                assert [m["tool_call_id"] for m in obj.messages if m["role"] == "tool"] == [
                    "one",
                    "two",
                ]
                assert sum(m["role"] == "user" for m in obj.messages) == 1
                assert obj._restored_system == "Private system context"
                await send({"type": "text", "content": "Done"})

            obj._execute_single_tool = execute
            obj._run = finish

        def hydrate_history(self, history):
            self._executor.messages = list(history)

        async def on_user_message(self, *args, **kwargs):
            pytest.fail("Resume must not submit the user prompt again")

    monkeypatch.setattr(orchestrator, "AgentOrchestrator", Agent)
    assert await execute_claimed_step(
        step_id, attempt_id, session_factory=factory, schedule_verification=False
    ) is (not repeat)
    assert effects == ["two"]
    if repeat:
        async with factory() as db:
            summary = await get_chat_checkpoint(run["id"], db, _DEV_USER)
            assert summary["can_resume"] is True
            assert summary["attempt_id"] == attempt_id
            assert summary["pending_tool_count"] == 1
    assert not await execute_claimed_step(
        step_id, attempt_id, session_factory=factory, schedule_verification=False
    )
    assert effects == ["two"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "foreign",
        "agent",
        "digest",
        "revision",
        "in_flight",
        "arguments",
        "config",
        "budget",
        "new_turn",
    ],
)
async def test_invalid_continuations_do_not_create_plans(test_engine, monkeypatch, case):
    from app.ai import agent_config

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, body = await stopped_confirmation(factory, monkeypatch)
    user = _DEV_USER
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        attempt = await db.get(WorkStepAttempt, body.attempt_id)
        if case == "foreign":
            user = user.model_copy(update={"sub": "foreign"})
        elif case == "agent":
            user = user.model_copy(update={"via_agent": True})
        elif case == "digest":
            body = body.model_copy(update={"sha256": "0" * 64})
        elif case == "revision":
            order.plan_revision += 1
        elif case in {"in_flight", "arguments"}:
            record = copy.deepcopy(attempt.checkpoint)
            payload = unpack_checkpoint(record["snapshot"])
            if case == "in_flight":
                payload["phase"] = "tool_started"
            else:
                payload["confirmation"]["args"] = {"id": "forged"}
            record["snapshot"] = pack_checkpoint(payload)
            attempt.checkpoint = record
            body = body.model_copy(update={"sha256": record["snapshot"]["sha256"]})
        elif case == "config":
            monkeypatch.setattr(
                agent_config, "get_builtin_agent_config", lambda: BuiltinAgentConfig(max_steps=99)
            )
        elif case == "budget":
            order.started_at = datetime.now(UTC) - timedelta(hours=3)
        elif case == "new_turn":
            await submit_chat_run(
                ChatRunCreate(
                    request_id=uuid.uuid4(), session_id=run["session_id"], content="New turn"
                ),
                db,
                user,
            )
        await db.commit()
    async with factory() as db:
        with pytest.raises(HTTPException) as denied:
            await resume_chat_run(run["id"], body, db, user)
        assert denied.value.status_code == {"foreign": 404, "agent": 403}.get(case, 409)
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkPlan)
                .where(WorkPlan.work_order_id == run["work_order_id"])
            )
            == 1
        )


@pytest.mark.asyncio
async def test_rejection_is_immutable_and_cannot_be_approved_later(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, body = await stopped_confirmation(factory, monkeypatch)
    rejected = body.model_copy(update={"approved": False})
    for _ in range(2):
        async with factory() as db:
            assert (await resume_chat_run(run["id"], rejected, db, _DEV_USER))[
                "status"
            ] == "blocked"
    async with factory() as db:
        assert (await get_chat_checkpoint(run["id"], db, _DEV_USER))["can_resume"] is False
        with pytest.raises(HTTPException) as denied:
            await resume_chat_run(run["id"], body, db, _DEV_USER)
        assert denied.value.status_code == 409


@pytest.mark.asyncio
async def test_expired_decision_stops_worker_before_agent_creation(test_engine, monkeypatch):
    from unittest.mock import Mock

    from app.ai import orchestrator
    from app.tasks.work_orders import execute_claimed_step

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, body = await stopped_confirmation(factory, monkeypatch)
    async with factory() as db:
        await resume_chat_run(run["id"], body, db, _DEV_USER)
    async with factory() as db:
        event = await db.scalar(
            select(WorkEvent).where(
                WorkEvent.work_order_id == run["work_order_id"],
                WorkEvent.event_type == "chat.continuation_decided",
            )
        )
        event.payload = {
            **event.payload,
            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        _, step, attempt = await claim_ready_step(
            db, worker_id="expired-worker", work_order_id=run["work_order_id"]
        )
        await db.commit()
    agent = Mock()
    monkeypatch.setattr(orchestrator, "AgentOrchestrator", agent)
    assert not await execute_claimed_step(step.id, attempt.id, session_factory=factory)
    agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_phase", ["tool_started", "tool_recorded"])
async def test_worker_journal_failure_fences_effect_and_preserves_unknown_outcome(
    test_engine, monkeypatch, failed_phase
):
    from app.ai import orchestrator
    from app.api.chat_runs import get_chat_actions
    from app.domain import chat_action_journal
    from app.tasks.work_orders import execute_claimed_step

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, attempt_id = await claim(factory)
    effects = []
    original = chat_action_journal.record_boundary

    async def failing_journal(db, order, attempt, payload):
        await original(db, order, attempt, payload)
        if payload["phase"] == failed_phase:
            raise ConnectionError("Simulated journal commit failure")

    class Agent:
        def __init__(self, send):
            self._executor = session(send)

            async def execute(tc, iteration):
                effects.append(tc["id"])
                return "test", {"result": tc["id"]}, tc["id"]

            self._executor._execute_single_tool = execute

        def hydrate_history(self, history):
            self._executor.messages = list(history)

        async def on_user_message(self, prompt, **kwargs):
            calls = [call(), call("two")]
            self._executor.messages += [
                {"role": "user", "content": prompt},
                {"role": "assistant", "tool_calls": calls},
            ]
            await self._executor._execute_tools_sequential(calls, 0)

    monkeypatch.setattr(orchestrator, "AgentOrchestrator", Agent)
    monkeypatch.setattr(chat_action_journal, "record_boundary", failing_journal)
    assert not await execute_claimed_step(step_id, attempt_id, session_factory=factory)
    assert effects == ([] if failed_phase == "tool_started" else ["one"])
    async with factory() as db:
        page = await get_chat_actions(run["id"], 0, 100, db, _DEV_USER)
        states = {item["call_id"]: item["status"] for item in page["items"]}
        assert states == {
            "one": "planned" if failed_phase == "tool_started" else "outcome_unknown",
            "two": "planned",
        }
        assert all(
            item["result_available"] is False and item["can_replay"] is False
            for item in page["items"]
        )
        assert (await db.get(WorkOrder, run["work_order_id"])).status == "blocked"
