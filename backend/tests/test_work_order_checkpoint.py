"""Ф1.B checkpoint propagation (AGENT_AUTONOMY_ROADMAP.md) — a capability
that fails partway through real progress can report it via
PartialProgressError; execute_claimed_step persists it on the failed attempt
and hands it to the next retry as ``_resume_checkpoint``.

Split out of test_work_order_lease.py's bucket (see that file's docstring for
the sibling split): this one is about execution/retry, not budget
housekeeping, so it gets its own file rather than growing an unrelated one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.chat_checkpoint import ChatNonterminalToolResult, pack_checkpoint
from app.db.agent_runtime_models import ChatLogicalAction
from app.db.models import (
    Approval,
    ApprovalActionType,
    WorkOrder,
    WorkStep,
    WorkStepAttempt,
    WorkToolCall,
)
from app.domain.work_orders import (
    claim_ready_step,
    complete_attempt,
    create_single_step_plan,
    create_work_order,
    create_work_plan,
    fail_attempt,
    utcnow,
)
from app.tasks.work_orders import (
    ApprovalRequiredError,
    NonterminalToolResultError,
    PartialProgressError,
    _action_digest,
    _execute_capability,
    _heartbeat_step,
    execute_claimed_step,
    verify_completed_step,
)


@pytest.mark.asyncio
async def test_versioned_waiting_approval_persists_exact_result_and_current_call_binding(
    test_engine,
):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(
            db,
            owner_key="tester",
            objective="Wait only for the exact requested capability action",
            budgets={"max_replans": 9},
        )
        plan, steps = await create_work_plan(
            db,
            order,
            steps=[
                {
                    "step_key": "call_a",
                    "title": "Call A",
                    "kind": "capability",
                    "capability": "email",
                    "action": "send",
                    "input": {"draft_id": "draft-a"},
                },
                {
                    "step_key": "call_b",
                    "title": "Call B must remain pending",
                    "kind": "capability",
                    "capability": "email",
                    "action": "send",
                    "input": {"draft_id": "draft-b"},
                    "depends_on": ["call_a"],
                },
            ],
        )
        order_id, step_id, dependent_id = order.id, steps[0].id, steps[1].id
        plan_revision = order.plan_revision
        await db.commit()

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        assert claimed is not None
        _order, _step, attempt = claimed
        attempt_id = attempt.id
        await db.commit()

    result = {
        "version": 1,
        "status": "waiting_approval",
        "data": {"recipient": "requires_approval"},
        "error_code": "approval_required",
        "retryable": False,
        "evidence": {
            "adapter_contract": "reviewed_v1",
            "recipient_supplied_digest": "must-not-be-trusted",
        },
        "checkpoint": {"request_id": "approval-1"},
    }
    current_arguments = {"draft_id": "draft-a", "work_order_id": str(order_id)}
    capability_call = AsyncMock(
        side_effect=ApprovalRequiredError("email", "send", current_arguments, tool_result=result)
    )
    verifier = AsyncMock()
    with (
        patch("app.tasks.work_orders._execute_step_kind", new=capability_call),
        patch("app.tasks.work_orders.verify_completed_step", new=verifier),
    ):
        completed = await execute_claimed_step(
            step_id, attempt_id, schedule_verification=False, session_factory=factory
        )

    assert completed is False
    assert capability_call.await_count == 1
    verifier.assert_not_awaited()
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        step = await db.get(WorkStep, step_id)
        dependent = await db.get(WorkStep, dependent_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        call = (
            await db.execute(select(WorkToolCall).where(WorkToolCall.attempt_id == attempt_id))
        ).scalar_one()
        approval = (
            await db.execute(select(Approval).where(Approval.entity_id == order_id))
        ).scalar_one()
        expected_digest = _action_digest("email", "send", current_arguments)

        assert order.status == "waiting_approval"
        assert order.plan_revision == plan_revision
        assert step.state == "waiting_approval"
        assert dependent.state == "pending"
        assert attempt.status == "waiting_approval"
        assert attempt.output == result
        assert attempt.checkpoint == result["checkpoint"]
        assert attempt.error["error_code"] == "approval_required"
        assert call.status == "waiting_approval"
        assert call.output == result
        assert call.error["evidence"] == result["evidence"]
        assert step.output == {"result": result, "executor": "capability"}
        assert approval.context["tool_name"] == "email"
        assert approval.context["action"] == "send"
        assert approval.context["tool_args"] == current_arguments
        assert approval.context["action_digest"] == expected_digest
        assert approval.context["action_digest"] != result["evidence"]["recipient_supplied_digest"]
    assert approval.context["tool_result"] == result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "create",
        "compact_create",
        "reuse",
        "approval_context",
        "request_digest",
        "snapshot_payload",
        "snapshot_hash",
        "duplicate",
        "foreign_collision",
    ],
)
async def test_recorded_durable_waiting_approval_preserves_binding_and_never_replays_source(
    test_engine,
    case,
):
    from app.ai.chat_checkpoint import ChatWaitingApprovalToolResult
    from app.domain.chat_action_journal import digest

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(
            db,
            owner_key="tester",
            objective="Recorded recipient approval",
            source="durable_chat",
            budgets={"max_replans": 0},
        )
        plan, steps = await create_work_plan(
            db,
            order,
            steps=[{"step_key": "chat", "title": "chat", "kind": "agent_turn", "input": {}}],
        )
        order_id, step_id = order.id, steps[0].id
        await db.commit()
    async with factory() as db:
        _order, _step, attempt = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        attempt_id = attempt.id
        request = {"name": "agent__mcp", "arguments": '{"action":"send","draft_id":"d-1"}'}
        result = (
            {"version": 1, "status": "waiting_approval"}
            if case == "compact_create"
            else {
                "version": 1,
                "status": "waiting_approval",
                "data": {"recipient": "requires_approval"},
                "error_code": "approval_required",
                "retryable": False,
                "evidence": {"recipient_digest": "untrusted"},
                "checkpoint": {"recipient": "must_not_replace_source"},
            }
        )
        action = ChatLogicalAction(
            work_order_id=order_id,
            attempt_id=attempt_id,
            call_id="call-1",
            request=request,
            request_digest=digest(request),
            status="waiting_approval",
            result=result,
            result_digest=digest(result),
        )
        db.add(action)
        await db.flush()
        snapshot = pack_checkpoint(
            {
                "phase": "tool_recorded",
                "messages": [{"role": "tool", "tool_call_id": "call-1", "content": "recorded"}],
                "pending_calls": [],
                "in_flight_call_id": None,
                "action_ids": {"call-1": str(action.id)},
                "completed_call": {
                    "action_id": str(action.id),
                    "call_id": "call-1",
                    "result": result,
                },
            }
        )
        attempt.checkpoint = {
            "kind": "durable_chat",
            "owner_key": "tester",
            "work_order_id": str(order_id),
            "step_id": str(step_id),
            "attempt_id": str(attempt_id),
            "plan_id": str(plan.id),
            "plan_revision": 1,
            "snapshot": snapshot,
        }
        approval_context = {
            "continuation_mode": "recorded_recipient_result",
            "work_order_id": str(order_id),
            "source_step_id": str(step_id),
            "source_attempt_id": str(attempt_id),
            "source_plan_id": str(plan.id),
            "source_plan_revision": 1,
            "snapshot_sha256": snapshot["sha256"],
            "action_id": str(action.id),
            "call_id": "call-1",
            "tool_name": "agent__mcp",
            "tool_args": {"action": "send", "draft_id": "d-1"},
            "request_digest": digest(request),
            "result_digest": digest(result),
            "action_digest": _action_digest(
                "agent__mcp", "send", {"action": "send", "draft_id": "d-1"}
            ),
            "tool_result": result,
        }
        existing = None
        if case not in {"create", "compact_create"}:
            existing = Approval(
                action_type=ApprovalActionType.agent_tool_call,
                entity_type="foreign" if case == "foreign_collision" else "work_order",
                entity_id=order_id,
                requested_by="tester",
                context=dict(approval_context),
            )
            db.add(existing)
        if case == "approval_context":
            existing.context = {k: v for k, v in existing.context.items() if k != "tool_result"}
        if case == "request_digest":
            action.request_digest = "forged"
        if case == "snapshot_payload":
            attempt.checkpoint["snapshot"]["payload"]["completed_call"]["call_id"] = "forged"
        if case == "snapshot_hash":
            attempt.checkpoint["snapshot"]["sha256"] = "forged"
        if case == "duplicate":
            db.add(
                Approval(
                    action_type=ApprovalActionType.agent_tool_call,
                    entity_type="work_order",
                    entity_id=order_id,
                    requested_by="tester",
                    context=dict(approval_context),
                )
            )
        await db.commit()
        action_id = action.id
        approval_id = existing.id if existing is not None else None

    runner = AsyncMock(
        side_effect=ChatWaitingApprovalToolResult(
            result, action_id=str(action_id), call_id="call-1", function=request
        )
    )
    with patch("app.tasks.durable_chat.run_durable_chat", new=runner):
        assert not await execute_claimed_step(step_id, attempt_id, session_factory=factory)
    runner.assert_awaited_once()

    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        step = await db.get(WorkStep, step_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        action = await db.get(ChatLogicalAction, action_id)
        approvals = list(
            (await db.execute(select(Approval).where(Approval.entity_id == order_id))).scalars()
        )
        if case in {"create", "compact_create"}:
            assert len(approvals) == 1
        elif case == "reuse":
            assert len(approvals) == 1 and approvals[0].id == approval_id
        if case in {"create", "compact_create", "reuse"}:
            assert order.status == "waiting_approval" and step.state == "waiting_approval"
            assert (
                attempt.output == result
                and attempt.checkpoint["snapshot"]["sha256"] == snapshot["sha256"]
            )
            assert action.status == "waiting_approval" and action.result_digest == digest(result)
            assert approvals[0].context["request_digest"] == digest(request)
            if "evidence" in result:
                assert (
                    approvals[0].context["action_digest"] != result["evidence"]["recipient_digest"]
                )
        else:
            assert order.status == "blocked" and step.state == "failed"
            assert attempt.error["code"] == "recorded_recipient_approval_binding_invalid"


@pytest.mark.asyncio
async def test_versioned_waiting_approval_is_recognized_and_http_423_remains_compatible():
    waiting_result = {
        "version": 1,
        "status": "waiting_approval",
        "data": {"recipient": "requires_approval"},
        "error_code": "approval_required",
        "retryable": False,
        "evidence": {"adapter_contract": "reviewed_v1"},
        "checkpoint": {"request_id": "approval-1"},
    }
    client = AsyncMock()
    client.post = AsyncMock(return_value=_http_response(200, waiting_result))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch(
            "app.ai.agent_config.get_builtin_agent_config",
            return_value=MagicMock(backend_url="http://backend"),
        ),
        patch("app.ai.orchestrator._agent_headers", return_value={}),
    ):
        with pytest.raises(ApprovalRequiredError) as exc_info:
            await _execute_capability("email", "send", {"draft_id": "draft-a"}, 30)

    assert exc_info.value.arguments == {"draft_id": "draft-a"}
    assert exc_info.value.tool_result == waiting_result
    assert client.post.await_count == 1

    client.post = AsyncMock(return_value=_http_response(423, {"detail": "approval required"}))
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch(
            "app.ai.agent_config.get_builtin_agent_config",
            return_value=MagicMock(backend_url="http://backend"),
        ),
        patch("app.ai.orchestrator._agent_headers", return_value={}),
    ):
        with pytest.raises(ApprovalRequiredError) as http_423:
            await _execute_capability("email", "send", {"draft_id": "draft-a"}, 30)

    assert http_423.value.arguments == {"draft_id": "draft-a"}
    assert http_423.value.tool_result is None


@pytest.mark.asyncio
async def test_approval_digest_for_call_a_cannot_authorize_call_b():
    call_a_arguments = {"draft_id": "draft-a"}
    call_b_arguments = {"draft_id": "draft-b"}
    approval_for_a = {
        "approval_id": "approval-a",
        "action_digest": _action_digest("email", "send", call_a_arguments),
        "approved_by": "manager",
    }
    client = AsyncMock()
    client.post = AsyncMock(return_value=_http_response(423, {"detail": "approval required"}))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch(
            "app.ai.agent_config.get_builtin_agent_config",
            return_value=MagicMock(backend_url="http://backend"),
        ),
        patch("app.ai.orchestrator._agent_headers", return_value={"X-Internal-Agent": "1"}),
    ):
        with pytest.raises(ApprovalRequiredError):
            await _execute_capability(
                "email",
                "send",
                {**call_b_arguments, "approval": approval_for_a},
                30,
            )

    sent = client.post.await_args.kwargs
    assert sent["json"] == {"action": "send", **call_b_arguments}
    assert "X-Agent-Approval" not in sent["headers"]
    assert "X-Agent-Approval-Digest" not in sent["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,error_code,checkpoint",
    [
        ("partial", "job_queued", {"task_id": "job-7"}),
        ("outcome_unknown", "tool_outcome_unknown", {"receipt": "unconfirmed"}),
    ],
)
async def test_nonterminal_v1_tool_result_blocks_without_retry_or_downstream_execution(
    test_engine, status, error_code, checkpoint
):
    """A received lifecycle result never becomes successful work progress."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(
            db,
            owner_key="tester",
            objective="Do not continue after a nonterminal tool result",
            budgets={"max_replans": 9},
        )
        _plan, steps = await create_work_plan(
            db,
            order,
            steps=[
                {
                    "step_key": "dispatch",
                    "title": "Dispatch",
                    "kind": "capability",
                    "capability": "email",
                    "action": "send",
                    "input": {"draft_id": "draft-1"},
                },
                {
                    "step_key": "dependent",
                    "title": "Must remain pending",
                    "kind": "capability",
                    "capability": "documents",
                    "action": "list",
                    "input": {},
                    "depends_on": ["dispatch"],
                },
            ],
        )
        order_id, step_id = order.id, steps[0].id
        dependent_id = steps[1].id
        plan_revision = order.plan_revision
        await db.commit()

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        assert claimed is not None
        _order, _step, attempt = claimed
        attempt_id = attempt.id
        await db.commit()

    result = {
        "version": 1,
        "status": status,
        "data": {"recipient": "accepted"},
        "error_code": error_code,
        "retryable": False,
        "evidence": {"adapter_contract": "reviewed_v1", "receipt": "r-1"},
        "checkpoint": checkpoint,
    }
    capability_call = AsyncMock(side_effect=ChatNonterminalToolResult(result))
    verifier = AsyncMock()
    with (
        patch("app.tasks.work_orders._execute_step_kind", new=capability_call),
        patch("app.tasks.work_orders.verify_completed_step", new=verifier),
    ):
        completed = await execute_claimed_step(
            step_id, attempt_id, schedule_verification=False, session_factory=factory
        )

    assert completed is False
    assert capability_call.await_count == 1
    verifier.assert_not_awaited()
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        step = await db.get(WorkStep, step_id)
        dependent = await db.get(WorkStep, dependent_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        call = (
            await db.execute(select(WorkToolCall).where(WorkToolCall.attempt_id == attempt_id))
        ).scalar_one()
        assert order.status == "blocked"
        assert order.plan_revision == plan_revision
        assert order.blocker["code"] == f"tool_result_{status}"
        assert step.state == "failed"
        assert dependent.state == "pending"
        assert attempt.status == status
        assert attempt.output == result
        assert attempt.checkpoint == checkpoint
        assert call.status == status
        assert call.output == result
        assert call.error["evidence"] == result["evidence"]


@pytest.mark.asyncio
async def test_versioned_succeeded_and_legacy_capability_results_keep_success_path():
    response = _http_response(
        200,
        {
            "version": 1,
            "status": "succeeded",
            "data": {"id": "read-1"},
            "retryable": False,
            "evidence": {"adapter_contract": "reviewed_v1"},
        },
    )
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch(
            "app.ai.agent_config.get_builtin_agent_config",
            return_value=MagicMock(backend_url="http://backend"),
        ),
        patch("app.ai.orchestrator._agent_headers", return_value={}),
    ):
        versioned = await _execute_capability("documents", "list", {}, 30)

    assert versioned["result"]["status"] == "succeeded"
    assert client.post.await_count == 1

    legacy_response = _http_response(200, {"items": [{"id": "legacy-1"}]})
    client.post = AsyncMock(return_value=legacy_response)
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch(
            "app.ai.agent_config.get_builtin_agent_config",
            return_value=MagicMock(backend_url="http://backend"),
        ),
        patch("app.ai.orchestrator._agent_headers", return_value={}),
    ):
        legacy = await _execute_capability("documents", "list", {}, 30)

    assert legacy["result"] == {"items": [{"id": "legacy-1"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["partial", "outcome_unknown"])
async def test_versioned_nonterminal_capability_result_is_recognized_before_legacy_failure(
    status,
):
    response = _http_response(
        200,
        {
            "version": 1,
            "status": status,
            "data": {"recipient": "accepted"},
            "error_code": "job_queued" if status == "partial" else "tool_outcome_unknown",
            "retryable": False,
            "evidence": {"adapter_contract": "reviewed_v1"},
            "checkpoint": {"cursor": "receipt-1"},
        },
    )
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch(
            "app.ai.agent_config.get_builtin_agent_config",
            return_value=MagicMock(backend_url="http://backend"),
        ),
        patch("app.ai.orchestrator._agent_headers", return_value={}),
    ):
        with pytest.raises(NonterminalToolResultError) as exc_info:
            await _execute_capability("documents", "list", {}, 30)

    assert exc_info.value.result["status"] == status
    assert exc_info.value.result["checkpoint"] == {"cursor": "receipt-1"}
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_versioned_failed_does_not_become_a_retry_from_model_hint():
    response = _http_response(
        200,
        {
            "version": 1,
            "status": "failed",
            "data": {"reason": "recipient rejected"},
            "error_code": "recipient_rejected",
            "retryable": True,
            "evidence": {"adapter_contract": "reviewed_v1"},
            "checkpoint": {"legacy_retry_must_not_apply": True},
        },
    )
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch(
            "app.ai.agent_config.get_builtin_agent_config",
            return_value=MagicMock(backend_url="http://backend"),
        ),
        patch("app.ai.orchestrator._agent_headers", return_value={}),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            await _execute_capability("documents", "list", {}, 30)

    assert not isinstance(exc_info.value, PartialProgressError)
    assert client.post.await_count == 1


# ── Ф4-re: heartbeat renews leases without locking the shared WorkOrder ────


@pytest.mark.asyncio
async def test_heartbeat_step_renews_leases_without_with_for_update_on_the_order(
    test_engine,
):
    """Ф4-re (AGENT_AUTONOMY_ROADMAP.md): found live on the persistence
    re-verification pilot — a real Postgres deadlock among 4 concurrently
    executing sibling steps of the same plan (asyncpg.DeadlockDetectedError).
    _heartbeat_step's with_for_update=True on the shared parent WorkOrder
    (every 30s, one heartbeat per concurrently active sibling step) was the
    likely source: an exclusive lock reserved well before the write that
    needed one, for a value (order.lease_expires_at) nothing needs strictly
    serialized against a sibling's own heartbeat. This only pins the basic
    functional behaviour is unchanged after dropping it — a genuine
    concurrency/deadlock stress test would need a dedicated multi-connection
    setup (like test_concurrent_claim_only_one_worker_gets_the_ready_step
    in test_work_order_lease.py), not attempted here."""
    import asyncio

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(db, owner_key="local:alice", objective="heartbeat")
        await create_single_step_plan(
            db, order, kind="agent_turn", title="x", input_data={"prompt": "x"}
        )
        order_id = order.id
        await db.commit()

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        assert claimed is not None
        _order, step, attempt = claimed
        step_id, attempt_id, worker_id = step.id, attempt.id, "w1"
        original_step_lease = step.lease_expires_at
        original_order_lease = _order.lease_expires_at
        await db.commit()

    stop = asyncio.Event()
    task = asyncio.create_task(
        _heartbeat_step(
            work_order_id=order_id,
            step_id=step_id,
            attempt_id=attempt_id,
            worker_id=worker_id,
            stop=stop,
            interval_seconds=0.05,
            lease_seconds=120,
            session_factory=factory,
        )
    )
    await asyncio.sleep(0.2)  # let at least one 0.05s-interval tick land
    stop.set()
    await task

    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        order = await db.get(WorkOrder, order_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        assert step.lease_expires_at > original_step_lease
        assert order.lease_expires_at > original_order_lease
        assert attempt.heartbeat_at is not None


# ── Ф4: acting-user context set for the duration of capability execution ──


@pytest.mark.asyncio
async def test_execute_claimed_step_sets_acting_user_to_the_orders_owner(test_engine):
    """Ф4 finding: nothing in this durable runtime ever called set_acting_user
    before this — every capability call authenticated as the bare
    "agent-service" account (app.ai.actor_context's own documented fail-closed
    default), so any endpoint scoping by the WorkOrder's owner via
    get_effective_user (computer_use's execute/web_discover, Ф2.A) 404'd on
    every real WorkOrder. Asserts the context is bound to owner_key exactly
    while _execute_step_kind runs, and cleared afterwards — this worker
    process/event loop may go on to execute unrelated tasks next."""
    from app.ai.actor_context import get_acting_user

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(db, owner_key="local:alice", objective="x")
        await create_single_step_plan(
            db, order, kind="agent_turn", title="x", input_data={"prompt": "x"}
        )
        order_id = order.id
        await db.commit()

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        assert claimed is not None
        _order, step, attempt = claimed
        step_id, attempt_id = step.id, attempt.id
        await db.commit()

    observed: dict = {}

    async def _capture_acting_user(kind, input_data, timeout_seconds, **kwargs):
        observed["during"] = get_acting_user()
        return {"text": "готово"}

    assert get_acting_user() is None  # nothing bound before this test's own call
    with patch(
        "app.tasks.work_orders._execute_step_kind", new=AsyncMock(side_effect=_capture_acting_user)
    ):
        await execute_claimed_step(
            step_id, attempt_id, schedule_verification=False, session_factory=factory
        )

    assert observed["during"] == "local:alice"
    assert get_acting_user() is None  # cleared afterwards


@pytest.mark.asyncio
async def test_verify_completed_step_recovers_order_stuck_in_ready(test_engine):
    """Ф4 (AGENT_AUTONOMY_ROADMAP.md): defense-in-depth companion to
    domain.work_orders.unstick_ready_orders_with_stalled_active_plan — even
    if verify_completed_step is reached directly (not just via
    _dispatch_ready_work's housekeeping pass) for a succeeded step whose
    order ended up "ready" instead of "running", it must self-heal rather
    than bail out with a silent False forever (the order used to require
    exactly "running").
    """
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(db, owner_key="local:bob", objective="Recover from ready")
        await create_single_step_plan(
            db, order, kind="agent_turn", title="y", input_data={"prompt": "y"}
        )
        order_id = order.id
        await db.commit()

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        assert claimed is not None
        order_ref, step, attempt = claimed
        step_id = step.id
        await complete_attempt(
            db, order=order_ref, step=step, attempt=attempt, output={"text": "ok"}, actor="w1"
        )
        # Simulate the stuck state directly (the exact mechanism that
        # produces it live doesn't matter here, only that verify_completed_step
        # must recover from it): the order sits "ready" with nothing left to
        # claim, instead of "running".
        order_ref.status = "ready"
        await db.commit()

    result = await verify_completed_step(step_id, session_factory=factory)

    assert result is True
    async with factory() as db:
        order_check = await db.get(WorkOrder, order_id)
        assert order_check.status == "completed"


# ── Ф4: work_order_id auto-filled into capability step arguments ──────────


@pytest.mark.asyncio
async def test_capability_step_gets_work_order_id_auto_filled_when_the_plan_omits_it(test_engine):
    """Ф4 finding, live on the pilot: computer_use's parameter schema names
    work_order_id as required, but the reasoning model generating the plan
    consistently left it out of the step's input — every real web_discover
    call 422'd. The durable runtime already knows this value authoritatively
    (step.work_order_id); making the model responsible for perfectly
    echoing it back was needless fragility. Applies to every capability step,
    not just computer_use, since any future capability could need it too."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(db, owner_key="local:alice", objective="x")
        await create_single_step_plan(
            db,
            order,
            kind="capability",
            title="Discover",
            input_data={"queries": ["q"]},  # no work_order_id, as the model actually produced
            capability="computer_use",
            action="web_discover",
        )
        order_id = order.id
        await db.commit()

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        assert claimed is not None
        _order, step, attempt = claimed
        step_id, attempt_id = step.id, attempt.id
        await db.commit()

    captured: dict = {}

    async def _capture_input(kind, input_data, timeout_seconds, **kwargs):
        captured.update(input_data)
        return {"text": "готово"}

    with patch(
        "app.tasks.work_orders._execute_step_kind", new=AsyncMock(side_effect=_capture_input)
    ):
        await execute_claimed_step(
            step_id, attempt_id, schedule_verification=False, session_factory=factory
        )

    assert captured["work_order_id"] == str(order_id)
    assert captured["queries"] == ["q"]  # the plan's own input is untouched


@pytest.mark.asyncio
async def test_capability_step_explicit_work_order_id_is_not_overridden(test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(db, owner_key="local:alice", objective="x")
        await create_single_step_plan(
            db,
            order,
            kind="capability",
            title="Discover",
            input_data={"queries": ["q"], "work_order_id": "explicit-value"},
            capability="computer_use",
            action="web_discover",
        )
        order_id = order.id
        await db.commit()

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        assert claimed is not None
        _order, step, attempt = claimed
        step_id, attempt_id = step.id, attempt.id
        await db.commit()

    captured: dict = {}

    async def _capture_input(kind, input_data, timeout_seconds, **kwargs):
        captured.update(input_data)
        return {"text": "готово"}

    with patch(
        "app.tasks.work_orders._execute_step_kind", new=AsyncMock(side_effect=_capture_input)
    ):
        await execute_claimed_step(
            step_id, attempt_id, schedule_verification=False, session_factory=factory
        )

    assert captured["work_order_id"] == "explicit-value"


@pytest.mark.asyncio
async def test_agent_turn_step_does_not_get_a_work_order_id_injected(test_engine):
    """Only kind="capability" steps get this — agent_turn's input is a free-
    form prompt dict, not a capability argument set."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(db, owner_key="local:alice", objective="x")
        await create_single_step_plan(
            db, order, kind="agent_turn", title="x", input_data={"prompt": "x"}
        )
        order_id = order.id
        await db.commit()

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        assert claimed is not None
        _order, step, attempt = claimed
        step_id, attempt_id = step.id, attempt.id
        await db.commit()

    captured: dict = {}

    async def _capture_input(kind, input_data, timeout_seconds, **kwargs):
        captured.update(input_data)
        return {"text": "готово"}

    with patch(
        "app.tasks.work_orders._execute_step_kind", new=AsyncMock(side_effect=_capture_input)
    ):
        await execute_claimed_step(
            step_id, attempt_id, schedule_verification=False, session_factory=factory
        )

    assert "work_order_id" not in captured


def _http_response(status_code: int, json_body: dict | None = None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"{}" if json_body is not None else b""
    resp.text = text
    resp.json = MagicMock(return_value=json_body or {})
    return resp


# ── _execute_capability raises PartialProgressError with a checkpoint ──────


class TestExecuteCapabilityCheckpoint:
    @pytest.mark.asyncio
    async def test_4xx_with_checkpoint_raises_partial_progress_error(self):
        response = _http_response(
            422, {"error": "timed out", "checkpoint": {"fetched": ["a", "b"]}}
        )
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("httpx.AsyncClient", return_value=client),
            patch(
                "app.ai.agent_config.get_builtin_agent_config",
                return_value=MagicMock(backend_url="http://backend"),
            ),
            patch("app.ai.orchestrator._agent_headers", return_value={}),
        ):
            with pytest.raises(PartialProgressError) as exc_info:
                await _execute_capability("web_discover", "search", {}, 30)
        assert exc_info.value.checkpoint == {"fetched": ["a", "b"]}

    @pytest.mark.asyncio
    async def test_4xx_without_checkpoint_raises_plain_runtime_error(self):
        response = _http_response(422, text="bad request")
        response.json = MagicMock(side_effect=ValueError("not json"))
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("httpx.AsyncClient", return_value=client),
            patch(
                "app.ai.agent_config.get_builtin_agent_config",
                return_value=MagicMock(backend_url="http://backend"),
            ),
            patch("app.ai.orchestrator._agent_headers", return_value={}),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await _execute_capability("web_discover", "search", {}, 30)
        assert not isinstance(exc_info.value, PartialProgressError)

    @pytest.mark.asyncio
    async def test_5xx_with_checkpoint_raises_partial_progress_not_connection_error(self):
        response = _http_response(503, {"error": "upstream down", "checkpoint": {"n": 3}})
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("httpx.AsyncClient", return_value=client),
            patch(
                "app.ai.agent_config.get_builtin_agent_config",
                return_value=MagicMock(backend_url="http://backend"),
            ),
            patch("app.ai.orchestrator._agent_headers", return_value={}),
        ):
            with pytest.raises(PartialProgressError) as exc_info:
                await _execute_capability("web_discover", "search", {}, 30)
        assert exc_info.value.checkpoint == {"n": 3}

    @pytest.mark.asyncio
    async def test_200_error_body_with_checkpoint_raises_partial_progress_error(self):
        response = _http_response(
            200, {"error": "partial batch failure", "checkpoint": {"cursor": "page-3"}}
        )
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("httpx.AsyncClient", return_value=client),
            patch(
                "app.ai.agent_config.get_builtin_agent_config",
                return_value=MagicMock(backend_url="http://backend"),
            ),
            patch("app.ai.orchestrator._agent_headers", return_value={}),
        ):
            with pytest.raises(PartialProgressError) as exc_info:
                await _execute_capability("web_discover", "search", {}, 30)
        assert exc_info.value.checkpoint == {"cursor": "page-3"}

    @pytest.mark.asyncio
    async def test_200_error_body_without_checkpoint_raises_plain_runtime_error(self):
        response = _http_response(200, {"error": "no idea what happened"})
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("httpx.AsyncClient", return_value=client),
            patch(
                "app.ai.agent_config.get_builtin_agent_config",
                return_value=MagicMock(backend_url="http://backend"),
            ),
            patch("app.ai.orchestrator._agent_headers", return_value={}),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await _execute_capability("web_discover", "search", {}, 30)
        assert not isinstance(exc_info.value, PartialProgressError)


# ── fail_attempt persists the checkpoint ─────────────────────────────────


class TestFailAttemptCheckpoint:
    @pytest.mark.asyncio
    async def test_checkpoint_is_persisted_on_the_failed_attempt(self, db_session):
        order = await create_work_order(
            db_session, owner_key="tester", objective="Проверка checkpoint"
        )
        await create_single_step_plan(
            db_session,
            order,
            kind="capability",
            title="Discover",
            input_data={},
            capability="web_discover",
            action="search",
            max_attempts=3,
        )
        claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
        assert claimed is not None
        claimed_order, claimed_step, attempt = claimed

        await fail_attempt(
            db_session,
            order=claimed_order,
            step=claimed_step,
            attempt=attempt,
            error={"code": "partial_progress", "message": "timed out"},
            retryable=True,
            actor="w1",
            checkpoint={"fetched": ["a", "b"], "pending": ["c"]},
        )
        await db_session.flush()

        assert attempt.checkpoint == {"fetched": ["a", "b"], "pending": ["c"]}
        assert claimed_step.state == "retry_wait"

    @pytest.mark.asyncio
    async def test_no_checkpoint_argument_leaves_it_none(self, db_session):
        """Every pre-existing fail_attempt call site doesn't pass checkpoint —
        confirms that path is unchanged (None, not some other default)."""
        order = await create_work_order(
            db_session, owner_key="tester", objective="Обычный сбой без checkpoint"
        )
        await create_single_step_plan(
            db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": "x"}
        )
        claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
        assert claimed is not None
        claimed_order, claimed_step, attempt = claimed

        await fail_attempt(
            db_session,
            order=claimed_order,
            step=claimed_step,
            attempt=attempt,
            error={"code": "execution_error", "message": "boom"},
            retryable=False,
            actor="w1",
        )
        await db_session.flush()

        assert attempt.checkpoint is None


# ── execute_claimed_step: a retry resumes from the prior attempt's checkpoint ──


@pytest.mark.asyncio
async def test_retry_receives_prior_attempts_checkpoint_as_resume_checkpoint(test_engine):
    """End-to-end: attempt 1 fails with a checkpoint (PartialProgressError) →
    persisted → step retried → attempt 2's resolved input carries
    ``_resume_checkpoint`` with exactly what attempt 1 reported.
    """
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(
            db, owner_key="tester", objective="Возобновиться после частичного прогресса"
        )
        await create_single_step_plan(
            db,
            order,
            kind="capability",
            title="Discover",
            input_data={"query": "поставщики режущего инструмента"},
            capability="web_discover",
            action="search",
            max_attempts=3,
        )
        order_id = order.id
        await db.commit()

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        assert claimed is not None
        _order, step, attempt = claimed
        step_id, attempt_id = step.id, attempt.id
        await db.commit()

    checkpoint = {"fetched_urls": ["https://a.example"], "next_query": "page 2"}

    with patch(
        "app.tasks.work_orders._execute_step_kind",
        new=AsyncMock(side_effect=PartialProgressError("timed out", checkpoint=checkpoint)),
    ):
        result = await execute_claimed_step(
            step_id, attempt_id, schedule_verification=False, session_factory=factory
        )
    assert result is False

    async with factory() as db:
        from app.db.models import WorkStep, WorkStepAttempt

        step_row = await db.get(WorkStep, step_id)
        attempt_row = await db.get(WorkStepAttempt, attempt_id)
        assert step_row.state == "retry_wait"
        assert attempt_row.checkpoint == checkpoint
        # Force the retry to be immediately claimable — the exponential
        # backoff delay itself isn't what this test is about.
        step_row.next_attempt_at = utcnow()
        await db.commit()

    async with factory() as db:
        claimed2 = await claim_ready_step(db, worker_id="w2", work_order_id=order_id)
        assert claimed2 is not None
        _order2, step2, attempt2 = claimed2
        assert attempt2.attempt_no == 2
        step2_id, attempt2_id = step2.id, attempt2.id
        await db.commit()

    captured_input: dict = {}

    async def _capture_and_succeed(kind, input_data, timeout_seconds, **kwargs):
        captured_input.update(input_data)
        return {"text": "готово", "executor": "capability"}

    with patch(
        "app.tasks.work_orders._execute_step_kind", new=AsyncMock(side_effect=_capture_and_succeed)
    ):
        result2 = await execute_claimed_step(
            step2_id, attempt2_id, schedule_verification=False, session_factory=factory
        )
    assert result2 is True
    assert captured_input.get("_resume_checkpoint") == checkpoint
    # The step's own static input is untouched — resume_checkpoint is additive.
    assert captured_input.get("query") == "поставщики режущего инструмента"
