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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import WorkOrder, WorkStep, WorkStepAttempt
from app.domain.work_orders import (
    claim_ready_step,
    complete_attempt,
    create_single_step_plan,
    create_work_order,
    fail_attempt,
    transition_step,
    utcnow,
)
from app.tasks.work_orders import (
    PartialProgressError,
    _execute_capability,
    _heartbeat_step,
    execute_claimed_step,
    verify_completed_step,
)


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
            work_order_id=order_id, step_id=step_id, attempt_id=attempt_id,
            worker_id=worker_id, stop=stop, interval_seconds=0.05, lease_seconds=120,
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

    with patch("app.tasks.work_orders._execute_step_kind", new=AsyncMock(side_effect=_capture_input)):
        await execute_claimed_step(step_id, attempt_id, schedule_verification=False, session_factory=factory)

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

    with patch("app.tasks.work_orders._execute_step_kind", new=AsyncMock(side_effect=_capture_input)):
        await execute_claimed_step(step_id, attempt_id, schedule_verification=False, session_factory=factory)

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

    with patch("app.tasks.work_orders._execute_step_kind", new=AsyncMock(side_effect=_capture_input)):
        await execute_claimed_step(step_id, attempt_id, schedule_verification=False, session_factory=factory)

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
