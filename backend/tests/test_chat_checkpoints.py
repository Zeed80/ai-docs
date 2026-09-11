"""Checkpoint integrity and tool-boundary persistence without automatic replay."""

import copy
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.agent_config import BuiltinAgentConfig
from app.ai.agent_loop import AgentSession
from app.ai.chat_checkpoint import ChatCheckpointError, pack_checkpoint, unpack_checkpoint
from app.api.chat_runs import ChatRunCreate, get_chat_checkpoint, submit_chat_run
from app.auth.jwt import _DEV_USER
from app.db.models import WorkOrder, WorkStepAttempt
from app.domain.work_orders import claim_ready_step


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
