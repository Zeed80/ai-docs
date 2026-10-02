"""E21.2b1 physical nested-tool attempts share the durable budget ledger."""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.agent_loop import AgentSession
from app.ai.work_budget_context import BudgetExecutionStopped, WorkBudgetContext
from app.api.chat_runs import (
    ChatResumeRequest,
    ChatRunCreate,
    get_chat_actions,
    get_chat_checkpoint,
    resume_chat_run,
    submit_chat_run,
)
from app.auth.jwt import _DEV_USER
from app.db.agent_runtime_models import ChatLogicalAction
from app.db.models import WorkOrder
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import claim_ready_step, create_single_step_plan, create_work_order

READ_SKILL = {"name": "documents", "method": "POST", "path": "/api/agent/cap/documents"}
READ_ARGS = {"action": "list"}
WRITE_SKILL = {"name": "documents", "method": "POST", "path": "/api/agent/cap/documents"}
WRITE_ARGS = {"action": "delete", "document_id": "document-1"}


async def _claimed_context(test_engine, *, max_tool_attempts: int | None = None):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        run = await submit_chat_run(
            ChatRunCreate(request_id=uuid.uuid4(), content="Budget tool test"),
            db,
            _DEV_USER,
        )
        if max_tool_attempts is not None:
            order = await db.get(WorkOrder, run["work_order_id"])
            ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id)
            ledger.max_tool_attempts = max_tool_attempts
            await db.commit()
    async with factory() as db:
        _, step, attempt = await claim_ready_step(
            db,
            worker_id="budget-tool-test",
            work_order_id=run["work_order_id"],
        )
        await db.commit()
    return (
        factory,
        run,
        WorkBudgetContext(
            work_order_id=run["work_order_id"],
            step_id=step.id,
            attempt_id=attempt.id,
            session_factory=factory,
        ),
    )


def _install_http(monkeypatch, outcomes, effects):
    remaining = list(outcomes)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def _dispatch(self, method, url, **kwargs):
            effects.append((method, url, kwargs))
            outcome = remaining.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            if callable(outcome):
                return await outcome()
            return outcome

        async def get(self, url, **kwargs):
            return await self._dispatch("GET", url, **kwargs)

        async def post(self, url, **kwargs):
            return await self._dispatch("POST", url, **kwargs)

        async def patch(self, url, **kwargs):
            return await self._dispatch("PATCH", url, **kwargs)

        async def delete(self, url, **kwargs):
            return await self._dispatch("DELETE", url, **kwargs)

    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda *args, **kwargs: Client())
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})


async def _tool_reservations(factory, order_id):
    async with factory() as db:
        return list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.work_order_id == order_id,
                    WorkBudgetReservation.dimension == "tool_attempts",
                )
            )
        )


@pytest.mark.asyncio
async def test_zero_tool_budget_stops_before_http_effect(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine, max_tool_attempts=0)
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"items": []})], effects)

    with pytest.raises(BudgetExecutionStopped) as stopped:
        await agent_loop.execute_skill(
            READ_SKILL,
            READ_ARGS,
            BuiltinAgentConfig(),
            budget_context=context,
        )

    assert stopped.value.code == "tool_attempt_budget_exceeded"
    assert effects == []
    assert await _tool_reservations(factory, run["work_order_id"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unsupported_method", "headers", "client_enter"])
async def test_local_transport_preflight_failures_cost_zero(test_engine, monkeypatch, mode):
    factory, run, context = await _claimed_context(test_engine)
    effects = []
    skill = READ_SKILL
    if mode == "unsupported_method":
        skill = {**READ_SKILL, "method": "PUT"}

        def forbidden_client(*args, **kwargs):
            raise AssertionError("client must not be constructed")

        monkeypatch.setattr(agent_loop.httpx, "AsyncClient", forbidden_client)
    elif mode == "headers":
        monkeypatch.setattr(
            agent_loop,
            "internal_headers",
            lambda: (_ for _ in ()).throw(RuntimeError("headers unavailable")),
        )

        def forbidden_client(*args, **kwargs):
            raise AssertionError("client must not be constructed")

        monkeypatch.setattr(agent_loop.httpx, "AsyncClient", forbidden_client)
    else:

        class Client:
            async def __aenter__(self):
                raise RuntimeError("client unavailable")

            async def __aexit__(self, *args):
                return None

        monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})
        monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    result = await agent_loop.execute_skill(
        skill,
        READ_ARGS,
        BuiltinAgentConfig(),
        budget_context=context,
    )

    assert result.get("error") or result.get("status") == "failed"
    assert effects == []
    assert await _tool_reservations(factory, run["work_order_id"]) == []


@pytest.mark.asyncio
async def test_reviewed_read_retry_charges_each_physical_attempt(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    effects = []
    _install_http(
        monkeypatch,
        [
            httpx.ReadTimeout("read failed"),
            httpx.Response(200, json={"items": ["one"]}),
        ],
        effects,
    )

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(agent_loop.asyncio, "sleep", no_sleep)

    result = await agent_loop.execute_skill(
        READ_SKILL,
        READ_ARGS,
        BuiltinAgentConfig(),
        budget_context=context,
    )

    assert result["status"] == "succeeded"
    assert len(effects) == 2
    reservations = await _tool_reservations(factory, run["work_order_id"])
    assert len(reservations) == 2
    assert {row.state for row in reservations} == {"charged"}


@pytest.mark.asyncio
async def test_write_transport_unknown_is_charged_once_without_retry(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    effects = []
    _install_http(
        monkeypatch,
        [httpx.ReadTimeout("recipient may have committed")],
        effects,
    )

    result = await agent_loop.execute_skill(
        WRITE_SKILL,
        WRITE_ARGS,
        BuiltinAgentConfig(),
        budget_context=context,
    )

    assert result["status"] == "outcome_unknown"
    assert result["retryable"] is False
    assert len(effects) == 1
    reservations = await _tool_reservations(factory, run["work_order_id"])
    assert len(reservations) == 1
    assert reservations[0].state == "charged"


@pytest.mark.asyncio
async def test_settlement_failure_keeps_known_result_then_stops(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"items": ["known"]})], effects)

    async def broken_settlement(*args, **kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr("app.ai.work_budget_context.settle_budget", broken_settlement)
    result = await agent_loop.execute_skill(
        READ_SKILL,
        READ_ARGS,
        BuiltinAgentConfig(),
        budget_context=context,
    )

    assert result["status"] == "succeeded"
    assert result["data"] == {"items": ["known"]}
    with pytest.raises(BudgetExecutionStopped) as stopped:
        context.raise_if_stopped()
    assert stopped.value.code == "tool_budget_settlement_unavailable"
    assert stopped.value.details["recipient_outcome"] == "responded"
    reservations = await _tool_reservations(factory, run["work_order_id"])
    assert len(reservations) == 1
    assert reservations[0].state == "reserved"


@pytest.mark.asyncio
async def test_settlement_failure_prevents_reviewed_read_retry(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    effects = []
    _install_http(
        monkeypatch,
        [
            httpx.ReadTimeout("first read failed"),
            httpx.Response(200, json={"items": ["unsafe retry"]}),
        ],
        effects,
    )

    async def broken_settlement(*args, **kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr("app.ai.work_budget_context.settle_budget", broken_settlement)
    result = await agent_loop.execute_skill(
        READ_SKILL,
        READ_ARGS,
        BuiltinAgentConfig(),
        budget_context=context,
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "read_transport_failed"
    assert len(effects) == 1
    with pytest.raises(BudgetExecutionStopped):
        context.raise_if_stopped()
    reservations = await _tool_reservations(factory, run["work_order_id"])
    assert len(reservations) == 1
    assert reservations[0].state == "reserved"


@pytest.mark.asyncio
async def test_crash_is_charged_and_same_attempt_cannot_replay(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    effects = []

    class ToolCrash(BaseException):
        pass

    _install_http(monkeypatch, [ToolCrash("worker lost")], effects)
    with pytest.raises(ToolCrash):
        await agent_loop.execute_skill(
            WRITE_SKILL,
            WRITE_ARGS,
            BuiltinAgentConfig(),
            budget_context=context,
        )

    replay = WorkBudgetContext(
        work_order_id=context.work_order_id,
        step_id=context.step_id,
        attempt_id=context.attempt_id,
        session_factory=factory,
    )
    with pytest.raises(BudgetExecutionStopped) as stopped:
        await agent_loop.execute_skill(
            WRITE_SKILL,
            WRITE_ARGS,
            BuiltinAgentConfig(),
            budget_context=replay,
        )
    assert stopped.value.code == "llm_execution_already_started"
    assert len(effects) == 1
    reservations = await _tool_reservations(factory, run["work_order_id"])
    assert len(reservations) == 1
    assert reservations[0].state == "charged"


async def _shared_claimed_contexts(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        root = await create_work_order(
            db,
            owner_key="shared-tool-budget-owner",
            objective="root",
            budgets={"max_tool_attempts": 1},
        )
        child = await create_work_order(
            db,
            owner_key=root.owner_key,
            objective="child",
            parent_id=root.id,
        )
        await initialize_budget_ledger(db, root.id)
        for order in (root, child):
            await create_single_step_plan(
                db,
                order,
                kind="test",
                title="shared tool budget",
                input_data={},
            )
        root_id, child_id = root.id, child.id
        await db.commit()

    contexts = []
    for index, order_id in enumerate((root_id, child_id)):
        async with factory() as db:
            _, step, attempt = await claim_ready_step(
                db,
                worker_id=f"shared-tool-{index}",
                work_order_id=order_id,
            )
            await db.commit()
        contexts.append(
            WorkBudgetContext(
                work_order_id=order_id,
                step_id=step.id,
                attempt_id=attempt.id,
                session_factory=factory,
            )
        )
    return factory, (root_id, child_id), contexts


@pytest.mark.asyncio
async def test_parallel_fresh_attempts_share_last_parent_tool_slot(test_engine, monkeypatch):
    factory, order_ids, contexts = await _shared_claimed_contexts(test_engine)
    effects = []

    async def response():
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"deleted": True})

    _install_http(monkeypatch, [response], effects)
    results = await asyncio.gather(
        *(
            agent_loop.execute_skill(
                WRITE_SKILL,
                WRITE_ARGS,
                BuiltinAgentConfig(),
                budget_context=context,
            )
            for context in contexts
        ),
        return_exceptions=True,
    )

    assert len(effects) == 1
    failures = [result for result in results if isinstance(result, BudgetExecutionStopped)]
    assert len(failures) == 1
    assert failures[0].code == "tool_attempt_budget_exceeded"
    reservations = []
    for order_id in order_ids:
        reservations.extend(await _tool_reservations(factory, order_id))
    assert len(reservations) == 1
    assert reservations[0].state == "charged"


@pytest.mark.asyncio
async def test_lease_cancel_after_reserve_stops_before_http(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"items": []})], effects)
    from app.ai import work_budget_context

    real_reserve = work_budget_context.reserve_budget_for_dispatch

    async def reserve_then_cancel(*args, **kwargs):
        reservation, created = await real_reserve(*args, **kwargs)
        if kwargs.get("dimension") == "tool_attempts":
            async with factory() as db:
                order = await db.get(WorkOrder, run["work_order_id"])
                order.status = "canceled"
                await db.commit()
        return reservation, created

    monkeypatch.setattr(work_budget_context, "reserve_budget_for_dispatch", reserve_then_cancel)
    with pytest.raises(BudgetExecutionStopped) as stopped:
        await agent_loop.execute_skill(
            READ_SKILL,
            READ_ARGS,
            BuiltinAgentConfig(),
            budget_context=context,
        )

    assert stopped.value.code == "budget_execution_inactive"
    assert effects == []
    reservations = await _tool_reservations(factory, run["work_order_id"])
    assert len(reservations) == 1
    assert reservations[0].state == "reserved"


@pytest.mark.asyncio
async def test_legacy_unbound_context_fails_closed_before_http(test_engine, monkeypatch):
    factory, run, context = await _claimed_context(test_engine)
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        order.budget_ledger_id = None
        await db.commit()
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"items": []})], effects)

    with pytest.raises(BudgetExecutionStopped) as stopped:
        await agent_loop.execute_skill(
            READ_SKILL,
            READ_ARGS,
            BuiltinAgentConfig(),
            budget_context=context,
        )

    assert stopped.value.code == "legacy_budget_baseline_required"
    assert effects == []


@pytest.mark.asyncio
async def test_approval_pause_costs_zero_then_authorized_dispatch_costs_one(
    test_engine, monkeypatch
):
    from app.ai import agent_config

    factory, run, context = await _claimed_context(test_engine)
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"deleted": True})], effects)
    config = BuiltinAgentConfig(approval_gates=["documents"])
    monkeypatch.setattr(agent_config, "get_builtin_agent_config", lambda: config)
    events = []

    async def send(event):
        events.append(event)

    session = agent_loop.AgentSession(send)
    session.set_work_budget_context(context)
    session._skill_map = {"documents__delete": WRITE_SKILL}

    async def no_log(**kwargs):
        return None

    session._log_action = no_log

    class ApprovalPaused(BaseException):
        pass

    async def pause(*args, **kwargs):
        raise ApprovalPaused()

    session._request_approval = pause
    tool_call = {
        "id": "delete-1",
        "function": {"name": "documents__delete", "arguments": WRITE_ARGS},
    }
    with pytest.raises(ApprovalPaused):
        await session._execute_single_tool(tool_call, 0)
    assert effects == []
    assert await _tool_reservations(factory, run["work_order_id"]) == []

    async def approve(*args, **kwargs):
        return True

    session._request_approval = approve
    _, result, _ = await session._execute_single_tool(tool_call, 0)

    assert result == {"deleted": True}
    assert len(effects) == 1
    reservations = await _tool_reservations(factory, run["work_order_id"])
    assert len(reservations) == 1
    assert reservations[0].state == "charged"


@pytest.mark.asyncio
async def test_unknown_result_survives_settlement_stop_and_cannot_blindly_resume(
    test_engine, monkeypatch
):
    from fastapi import HTTPException

    from app.ai import orchestrator
    from app.db.models import WorkStepAttempt
    from app.tasks.work_orders import execute_claimed_step

    factory, run, context = await _claimed_context(test_engine)
    effects = []
    _install_http(
        monkeypatch,
        [httpx.ReadTimeout("recipient outcome is unknown")],
        effects,
    )

    async def broken_settlement(*args, **kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr("app.ai.work_budget_context.settle_budget", broken_settlement)

    class Agent:
        def __init__(self, send):
            self._executor = AgentSession(send)
            self._executor._skill_map = {"documents__delete": WRITE_SKILL}

            async def no_log(**kwargs):
                return None

            self._executor._log_action = no_log

        def hydrate_history(self, history):
            self._executor.messages = list(history)

        async def on_user_message(self, prompt, **kwargs):
            call = {
                "id": "unknown-write",
                "function": {"name": "documents__delete", "arguments": WRITE_ARGS},
            }
            self._executor.messages.extend(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "tool_calls": [call]},
                ]
            )
            await self._executor._execute_tools_sequential([call], 0)

    monkeypatch.setattr(orchestrator, "AgentOrchestrator", Agent)
    assert not await execute_claimed_step(
        context.step_id,
        context.attempt_id,
        schedule_verification=False,
        session_factory=factory,
    )
    assert len(effects) == 1

    async with factory() as db:
        action = await db.scalar(
            select(ChatLogicalAction).where(
                ChatLogicalAction.work_order_id == run["work_order_id"],
                ChatLogicalAction.call_id == "unknown-write",
            )
        )
        assert action.status == "outcome_unknown"
        assert action.result["status"] == "outcome_unknown"
        action_page = await get_chat_actions(run["id"], 0, 100, db, _DEV_USER)
        saved_action = next(
            item for item in action_page["items"] if item["call_id"] == action.call_id
        )
        assert saved_action["can_replay"] is False
        summary = await get_chat_checkpoint(run["id"], db, _DEV_USER)
        assert summary["can_resume"] is False
        attempt = await db.get(WorkStepAttempt, context.attempt_id)
        snapshot = attempt.checkpoint["snapshot"]
        body = ChatResumeRequest(
            attempt_id=context.attempt_id,
            sha256=snapshot["sha256"],
            approved=True,
        )
        with pytest.raises(HTTPException) as denied:
            await resume_chat_run(run["id"], body, db, _DEV_USER)
        assert denied.value.status_code == 409


@pytest.mark.asyncio
async def test_worker_persists_tool_budget_exhaustion_as_explicit_blocker(test_engine, monkeypatch):
    from app.ai import orchestrator
    from app.db.models import WorkStep, WorkStepAttempt
    from app.tasks.work_orders import execute_claimed_step

    factory, run, context = await _claimed_context(test_engine, max_tool_attempts=0)
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"items": []})], effects)

    class Agent:
        def __init__(self, send):
            self._executor = AgentSession(send)
            self._executor._skill_map = {"documents__list": READ_SKILL}

            async def no_log(**kwargs):
                return None

            self._executor._log_action = no_log

        def hydrate_history(self, history):
            self._executor.messages = list(history)

        async def on_user_message(self, prompt, **kwargs):
            call = {
                "id": "blocked-read",
                "function": {"name": "documents__list", "arguments": READ_ARGS},
            }
            self._executor.messages.extend(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "tool_calls": [call]},
                ]
            )
            await self._executor._execute_tools_sequential([call], 0)

    monkeypatch.setattr(orchestrator, "AgentOrchestrator", Agent)
    assert not await execute_claimed_step(
        context.step_id,
        context.attempt_id,
        schedule_verification=False,
        session_factory=factory,
    )
    assert effects == []
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        step = await db.get(WorkStep, context.step_id)
        attempt = await db.get(WorkStepAttempt, context.attempt_id)
        assert order.status == "blocked"
        assert order.blocker["code"] == "tool_attempt_budget_exceeded"
        assert step.state == "failed"
        assert attempt.status == "failed"


@pytest.mark.asyncio
async def test_known_tool_result_is_checkpointed_before_settlement_blocker(
    test_engine, monkeypatch
):
    from app.ai import orchestrator
    from app.ai.chat_checkpoint import unpack_checkpoint
    from app.db.models import WorkStepAttempt
    from app.tasks.work_orders import execute_claimed_step

    factory, run, context = await _claimed_context(test_engine)
    effects = []
    _install_http(monkeypatch, [httpx.Response(200, json={"items": ["known"]})], effects)

    async def broken_settlement(*args, **kwargs):
        raise RuntimeError("budget database unavailable")

    monkeypatch.setattr("app.ai.work_budget_context.settle_budget", broken_settlement)

    class Agent:
        def __init__(self, send):
            self._executor = AgentSession(send)
            self._executor._skill_map = {"documents__list": READ_SKILL}

            async def no_log(**kwargs):
                return None

            self._executor._log_action = no_log

        def hydrate_history(self, history):
            self._executor.messages = list(history)

        async def on_user_message(self, prompt, **kwargs):
            call = {
                "id": "known-read",
                "function": {"name": "documents__list", "arguments": READ_ARGS},
            }
            self._executor.messages.extend(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "tool_calls": [call]},
                ]
            )
            await self._executor._execute_tools_sequential([call], 0)

    monkeypatch.setattr(orchestrator, "AgentOrchestrator", Agent)
    assert not await execute_claimed_step(
        context.step_id,
        context.attempt_id,
        schedule_verification=False,
        session_factory=factory,
    )
    assert len(effects) == 1
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        attempt = await db.get(WorkStepAttempt, context.attempt_id)
        action = await db.scalar(
            select(ChatLogicalAction).where(
                ChatLogicalAction.work_order_id == run["work_order_id"],
                ChatLogicalAction.call_id == "known-read",
            )
        )
        checkpoint = unpack_checkpoint(attempt.checkpoint["snapshot"])
        assert action.status == "result_recorded"
        assert action.result["data"] == {"items": ["known"]}
        assert checkpoint["phase"] == "tool_recorded"
        assert checkpoint["completed_call"]["result"] == action.result
        assert checkpoint["can_resume"] is False
        assert order.status == "blocked"
        assert order.blocker["code"] == "tool_budget_settlement_unavailable"
