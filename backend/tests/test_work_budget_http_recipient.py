"""Signed SQL-table recipient shares the parent budget and cannot replay."""

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

from app.ai import agent_loop, ollama_client, work_budget_context
from app.ai.actor_context import get_acting_user, set_acting_user
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.work_budget_context import BudgetExecutionStopped, bind_http_recipient_budget_context
from app.api import workspace
from app.auth.execution_context import sign_execution_context
from app.auth.jwt import _DEV_USER
from app.auth.work_budget_handoff import (
    WORK_BUDGET_HANDOFF_HEADER,
    WORKSPACE_SQL_TABLE_PATH,
    resolve_sql_recipient_context,
    sign_work_budget_handoff,
    verify_work_budget_handoff,
)
from app.config import settings
from app.db.models import User, WorkOrder, WorkPlan, WorkStep
from app.db.work_budget_models import WorkBudgetReservation
from tests.test_work_budget_direct_text import (
    _claimed_context,
    _install_http,
    _install_local_runtime,
    _Response,
)

BODY = {"task": "List invoices", "canvas_id": "test:recipient"}
SKILL = {"name": "workspace.sql_table", "method": "POST", "path": WORKSPACE_SQL_TABLE_PATH}


def _request(headers, body=None, *, path=WORKSPACE_SQL_TABLE_PATH):
    raw = json.dumps(BODY if body is None else body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "query_string": b"",
            "server": ("test", 80),
            "scheme": "http",
        },
        receive,
    )


async def _setup(test_engine, monkeypatch, *, budgets=None):
    initial_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with initial_factory() as db:
        actor = await db.scalar(select(User).where(User.sub == _DEV_USER.sub))
        if actor is not None:
            actor.is_active = True
            await db.commit()
    factory, run, parent = await _claimed_context(test_engine, budgets=budgets)
    async with factory() as db:
        actor = await db.scalar(select(User).where(User.sub == parent.expected_owner_key))
        if actor is None:
            db.add(
                User(
                    sub=parent.expected_owner_key,
                    email="recipient@example.test",
                    name="Recipient test",
                    preferred_username="recipient",
                    role="admin",
                )
            )
        else:
            actor.is_active = True
        await db.commit()
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    monkeypatch.setattr(settings, "agent_service_key", "recipient-test-service-key")
    monkeypatch.setattr(settings, "auth_enabled", False)
    operation = await parent.prepare_tool_attempt(
        method="POST",
        url="http://backend" + WORKSPACE_SQL_TABLE_PATH,
        request={"body": BODY},
    )
    token = await parent.create_http_recipient_handoff(
        method="POST",
        path=WORKSPACE_SQL_TABLE_PATH,
        body=BODY,
        tool_operation_key=operation,
        actor=parent.expected_owner_key,
    )
    headers = {
        "X-API-Key": settings.agent_service_key,
        "X-Execution-Context": sign_execution_context(parent.expected_owner_key),
        WORK_BUDGET_HANDOFF_HEADER: token,
    }
    return factory, run, parent, headers


def _result(response):
    return json.loads(response.body)


async def _rows(factory, order_id, prefix):
    async with factory() as db:
        return list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.work_order_id == order_id,
                    WorkBudgetReservation.operation_key.like(prefix + "%"),
                )
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["key", "signature", "expired", "body", "actor", "path", "missing"])
async def test_invalid_authority_blocks_before_model_even_with_auth_disabled(
    test_engine, monkeypatch, bad
):
    _, _, _, headers = await _setup(test_engine, monkeypatch)
    body = dict(BODY)
    path = WORKSPACE_SQL_TABLE_PATH
    if bad == "key":
        headers["X-API-Key"] = "wrong"
    elif bad == "signature":
        headers[WORK_BUDGET_HANDOFF_HEADER] += "x"
    elif bad == "expired":
        handoff = verify_work_budget_handoff(headers[WORK_BUDGET_HANDOFF_HEADER])
        headers[WORK_BUDGET_HANDOFF_HEADER] = sign_work_budget_handoff(
            handoff.model_copy(update={"expires_at": int(time.time()) - 1})
        )
    elif bad == "body":
        body["task"] = "Different request"
    elif bad == "actor":
        handoff = verify_work_budget_handoff(headers[WORK_BUDGET_HANDOFF_HEADER])
        headers[WORK_BUDGET_HANDOFF_HEADER] = sign_work_budget_handoff(
            handoff.model_copy(update={"actor": "other-owner"})
        )
    elif bad == "path":
        path += "/other"
    else:
        del headers[WORK_BUDGET_HANDOFF_HEADER]
    with pytest.raises(HTTPException):
        await resolve_sql_recipient_context(_request(headers, body, path=path))


@pytest.mark.asyncio
async def test_plain_service_and_unsigned_actor_are_not_manual_calls(test_engine, monkeypatch):
    await _setup(test_engine, monkeypatch)
    for headers in ({"X-API-Key": settings.agent_service_key}, {"X-Acting-User": "dev-user"}):
        with pytest.raises(HTTPException):
            await resolve_sql_recipient_context(_request(headers))
    assert await resolve_sql_recipient_context(_request({})) is None


@pytest.mark.asyncio
async def test_actual_route_sql_and_title_share_parent_ledger(test_engine, monkeypatch):
    factory, run, parent, headers = await _setup(test_engine, monkeypatch)
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(
        monkeypatch, [_Response("SELECT id FROM invoices"), _Response("Invoices")]
    )

    async def execute_sql(*_args, **_kwargs):
        return [{"id": "one"}]

    monkeypatch.setattr("app.ai.table_sql_pipeline.execute_sql", execute_sql)
    writes = []
    monkeypatch.setattr(
        workspace, "upsert_workspace_block", lambda key, block: writes.append(key) or block
    )

    async def no_publish(_event):
        pass

    monkeypatch.setattr(workspace.chat_bus, "publish", no_publish)
    set_acting_user("previous-actor")
    response = await workspace.publish_sql_table(
        workspace.WorkspaceSqlTableRequest(**BODY), _request(headers)
    )
    assert _result(response)["status"] == "succeeded"
    assert get_acting_user() == "previous-actor"
    set_acting_user(None)
    assert work_budget_context.current_airouter_budget_context() is None
    rows = await _rows(factory, run["work_order_id"], "recipient-llm:")
    assert len(posts) == len(rows) == 2
    assert writes == [BODY["canvas_id"]]
    assert {r.ledger_id for r in rows} == {parent.expected_ledger_id}
    assert {r.state for r in rows} == {"charged"}


@pytest.mark.asyncio
async def test_concurrent_duplicate_once_fence_has_one_winner_no_deadlock(test_engine, monkeypatch):
    _, _, _, headers = await _setup(test_engine, monkeypatch)
    contexts = [await resolve_sql_recipient_context(_request(headers)) for _ in range(2)]
    results = await asyncio.wait_for(
        asyncio.gather(*(c.assert_ready() for c in contexts), return_exceptions=True), timeout=5
    )
    assert sum(r is None for r in results) == 1
    stop = next(r for r in results if isinstance(r, BudgetExecutionStopped))
    assert stop.code == "http_recipient_already_started"
    assert stop.details["publication_state"] == "prior_outcome_unknown"


@pytest.mark.asyncio
async def test_once_fence_commit_failure_is_sticky_without_deadlock(test_engine, monkeypatch):
    _, _, _, headers = await _setup(test_engine, monkeypatch)
    context = await resolve_sql_recipient_context(_request(headers))

    async def unavailable(*args, **kwargs):
        raise RuntimeError("test database unavailable")

    monkeypatch.setattr(work_budget_context, "reserve_budget_for_dispatch", unavailable)
    with pytest.raises(BudgetExecutionStopped) as stop:
        await asyncio.wait_for(context.assert_ready(), timeout=5)
    assert stop.value.code == "http_recipient_fence_failed"
    with pytest.raises(BudgetExecutionStopped):
        context.raise_if_stopped()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["settled", "cancel", "revision", "actor", "binding"])
async def test_stale_recipient_stops_before_gpu_and_publication(test_engine, monkeypatch, change):
    factory, _, parent, headers = await _setup(test_engine, monkeypatch)
    context = await resolve_sql_recipient_context(_request(headers))
    await context.assert_ready()
    if change == "settled":
        await parent.charge_tool_attempt(
            context.tool_operation_key, recipient_outcome="unconfirmed"
        )
    elif change == "binding":
        context = replace(context, tool_binding_digest="0" * 64)
    else:
        async with factory() as db:
            if change == "cancel":
                row = await db.get(WorkOrder, parent.work_order_id)
                row.status = "cancelled"
            elif change == "revision":
                row = await db.get(WorkPlan, parent.expected_plan_id)
                row.revision += 1
            else:
                row = await db.scalar(select(User).where(User.sub == context.owner_key))
                row.is_active = False
            await db.commit()
    gpu = _install_local_runtime(monkeypatch)
    posts, _ = _install_http(monkeypatch, [])
    with bind_http_recipient_budget_context(context), pytest.raises(BudgetExecutionStopped):
        await ollama_client.reasoning_generate("test")
    assert posts == gpu == []
    with pytest.raises(BudgetExecutionStopped):
        async with context.publication_fence():
            pytest.fail("stale context reached store write")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budgets", [{"max_tokens": 100}, {"max_cost_usd": 1}, {"max_llm_calls": 0}]
)
async def test_bounds_stop_before_provider_preparation(test_engine, monkeypatch, budgets):
    _, _, _, headers = await _setup(test_engine, monkeypatch, budgets=budgets)
    context = await resolve_sql_recipient_context(_request(headers))
    gpu = _install_local_runtime(monkeypatch)
    posts, _ = _install_http(monkeypatch, [])
    with bind_http_recipient_budget_context(context), pytest.raises(BudgetExecutionStopped):
        await ollama_client.reasoning_generate("test")
    assert posts == []
    assert gpu == []


@pytest.mark.asyncio
async def test_parent_timeout_at_client_close_charges_model_but_stops_result(
    test_engine, monkeypatch
):
    factory, run, parent, headers = await _setup(test_engine, monkeypatch)
    context = await resolve_sql_recipient_context(_request(headers))
    _install_local_runtime(monkeypatch)

    async def parent_timeout():
        await parent.charge_tool_attempt(
            context.tool_operation_key, recipient_outcome="unconfirmed"
        )

    posts, _ = _install_http(monkeypatch, [_Response("known")], exit_callbacks=[parent_timeout])
    with bind_http_recipient_budget_context(context), pytest.raises(BudgetExecutionStopped) as stop:
        await ollama_client.generate("test", max_retries=0)
    assert stop.value.code == "http_recipient_execution_inactive"
    rows = await _rows(factory, run["work_order_id"], "recipient-llm:")
    assert len(posts) == len(rows) == 1
    assert rows[0].state == "charged"


@pytest.mark.asyncio
async def test_publication_fence_serializes_parent_timeout(test_engine, monkeypatch):
    _, _, parent, headers = await _setup(test_engine, monkeypatch)
    context = await resolve_sql_recipient_context(_request(headers))
    await context.assert_ready()
    async with context.publication_fence():
        settlement = asyncio.create_task(
            parent.charge_tool_attempt(context.tool_operation_key, recipient_outcome="unconfirmed")
        )
        await asyncio.sleep(0.05)
        assert not settlement.done()
    assert await asyncio.wait_for(settlement, timeout=5)
    with pytest.raises(BudgetExecutionStopped):
        await context.assert_publish_current()


@pytest.mark.asyncio
async def test_parent_saves_observed_stop_before_raising_and_does_not_retry(
    test_engine, monkeypatch
):
    _, _, parent, _ = await _setup(test_engine, monkeypatch)
    from tests.test_work_budget_tools import _install_http as install_parent_http

    effects = []
    payload = {
        "version": 1,
        "status": "failed",
        "retryable": False,
        "error_code": "llm_call_budget_exceeded",
        "evidence": {"budget_stop": True, "publication_state": "not_published"},
    }
    install_parent_http(monkeypatch, [httpx.Response(200, json=payload)], effects)
    set_acting_user(parent.expected_owner_key)
    snapshots = []

    async def send(_event):
        pass

    async def checkpoint(snapshot):
        from app.ai.chat_checkpoint import unpack_checkpoint

        snapshots.append(unpack_checkpoint(snapshot))

    session = agent_loop.AgentSession(send)
    session.set_work_budget_context(parent)
    session.set_checkpoint_sink(checkpoint)
    session._skill_map = {"workspace__sql_table": SKILL}

    async def no_log(**_kwargs):
        pass

    session._log_action = no_log
    call = {"id": "sql-receipt", "function": {"name": "workspace__sql_table", "arguments": BODY}}
    session.messages = [{"role": "assistant", "tool_calls": [call]}]
    try:
        with pytest.raises(BudgetExecutionStopped):
            await session._execute_tools_sequential([call], 0)
    finally:
        set_acting_user(None)
    assert len(effects) == 1
    assert snapshots[-1]["phase"] == "tool_recorded"
    assert snapshots[-1]["completed_call"]["result"]["error_code"] == payload["error_code"]


@pytest.mark.asyncio
async def test_fastapi_response_model_preserves_recipient_envelope(test_engine, monkeypatch):
    _, _, _, headers = await _setup(test_engine, monkeypatch, budgets={"max_tokens": 1})
    _install_local_runtime(monkeypatch)
    app = FastAPI()
    app.include_router(workspace.router, prefix="/api/workspace")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(WORKSPACE_SQL_TABLE_PATH, headers=headers, json=BODY)
    assert response.status_code == 200
    assert response.json()["evidence"]["budget_stop"] is True
    assert response.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_full_parent_http_route_and_two_model_leaves_share_ledger(test_engine, monkeypatch):
    factory, run, parent, _ = await _setup(test_engine, monkeypatch)
    _install_local_runtime(monkeypatch)
    responses = iter(["SELECT id FROM invoices", "Invoices"])
    physical = []
    writes = []

    async def execute_sql(*_args, **_kwargs):
        return [{"id": "one"}]

    async def publish(_event):
        pass

    monkeypatch.setattr("app.ai.table_sql_pipeline.execute_sql", execute_sql)
    monkeypatch.setattr(workspace.chat_bus, "publish", publish)
    monkeypatch.setattr(
        workspace, "upsert_workspace_block", lambda key, block: writes.append(key) or block
    )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, **kwargs):
            if url.endswith(WORKSPACE_SQL_TABLE_PATH):
                response = await workspace.publish_sql_table(
                    workspace.WorkspaceSqlTableRequest(**kwargs["json"]),
                    _request(kwargs["headers"], kwargs["json"]),
                )
                return httpx.Response(200, json=_result(response))
            physical.append(url)
            return _Response(next(responses))

    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda **_kwargs: Client())
    set_acting_user(parent.expected_owner_key)
    try:
        result = await agent_loop.execute_skill(
            SKILL, BODY, BuiltinAgentConfig(), budget_context=parent
        )
    finally:
        set_acting_user(None)
    assert result["status"] == "succeeded"
    assert writes == [BODY["canvas_id"]]
    rows = await _rows(factory, run["work_order_id"], "recipient-llm:")
    assert len(rows) == len(physical) == 2
    tools = await _rows(factory, run["work_order_id"], "tool:")
    assert sum(row.state == "charged" for row in tools) == 1


@pytest.mark.asyncio
async def test_recipient_model_retry_consumes_two_shared_slots(test_engine, monkeypatch):
    factory, run, _, headers = await _setup(test_engine, monkeypatch, budgets={"max_llm_calls": 2})
    context = await resolve_sql_recipient_context(_request(headers))
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(monkeypatch, [httpx.ConnectError("offline"), _Response("known")])

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr(ollama_client, "_async_sleep", no_sleep)
    with bind_http_recipient_budget_context(context):
        result = await ollama_client.generate("test", max_retries=1)
    assert result.text == "known"
    rows = await _rows(factory, run["work_order_id"], "recipient-llm:")
    assert len(posts) == len(rows) == 2
    assert {row.state for row in rows} == {"charged"}


@pytest.mark.asyncio
async def test_optional_title_stop_does_not_publish_or_fallback(test_engine, monkeypatch):
    _, _, _, headers = await _setup(test_engine, monkeypatch, budgets={"max_llm_calls": 1})
    _install_local_runtime(monkeypatch)
    posts, _ = _install_http(monkeypatch, [_Response("SELECT id FROM invoices")])

    async def execute_sql(*_args, **_kwargs):
        return [{"id": "one"}]

    monkeypatch.setattr("app.ai.table_sql_pipeline.execute_sql", execute_sql)
    writes = []
    monkeypatch.setattr(workspace, "upsert_workspace_block", lambda *args: writes.append(args))
    response = await workspace.publish_sql_table(
        workspace.WorkspaceSqlTableRequest(**BODY), _request(headers)
    )
    assert _result(response)["error_code"] == "llm_call_budget_exceeded"
    assert _result(response)["evidence"]["publication_state"] == "not_published"
    assert len(posts) == 1
    assert writes == []


@pytest.mark.asyncio
async def test_publication_does_not_deadlock_step_first_heartbeat(test_engine, monkeypatch):
    factory, _, parent, headers = await _setup(test_engine, monkeypatch)
    context = await resolve_sql_recipient_context(_request(headers))
    await context.assert_ready()
    async with factory() as heartbeat:
        async with heartbeat.begin():
            await heartbeat.get(WorkStep, parent.step_id, with_for_update=True)

            # A heartbeat can hold the step before it touches the order.
            async def guarded_write():
                async with context.publication_fence():
                    return "published"

            assert await asyncio.wait_for(guarded_write(), timeout=5) == "published"


@pytest.mark.asyncio
async def test_generic_capability_sql_table_is_fail_closed_before_transport(monkeypatch):
    from app.tasks.work_orders import _execute_capability

    def no_client(**_kwargs):
        pytest.fail("unmigrated proxy reached HTTP preparation")

    monkeypatch.setattr(httpx, "AsyncClient", no_client)
    with pytest.raises(BudgetExecutionStopped) as stop:
        await _execute_capability("workspace", "sql_table", BODY, timeout_seconds=30)
    assert stop.value.code == "workspace_sql_table_recipient_handoff_required"


@pytest.mark.asyncio
async def test_parent_auth_failure_returns_receipt_then_sticky_stop(test_engine, monkeypatch):
    _, _, parent, _ = await _setup(test_engine, monkeypatch)
    from tests.test_work_budget_tools import _install_http as install_parent_http

    effects = []
    install_parent_http(
        monkeypatch, [httpx.Response(403, json={"detail": "expired handoff"})], effects
    )
    set_acting_user(parent.expected_owner_key)
    try:
        result = await agent_loop.execute_skill(
            SKILL, BODY, BuiltinAgentConfig(), budget_context=parent
        )
    finally:
        set_acting_user(None)
    assert result["status"] == "failed"
    assert result["error_code"] == "workspace_sql_table_http_403"
    assert len(effects) == 1
    with pytest.raises(BudgetExecutionStopped):
        parent.raise_if_stopped()


@pytest.mark.asyncio
async def test_duplicate_after_parent_settlement_does_not_claim_no_prior_publication(
    test_engine, monkeypatch
):
    _, _, parent, headers = await _setup(test_engine, monkeypatch)
    context = await resolve_sql_recipient_context(_request(headers))
    await context.assert_ready()
    await parent.charge_tool_attempt(context.tool_operation_key, recipient_outcome="responded")
    response = await workspace.publish_sql_table(
        workspace.WorkspaceSqlTableRequest(**BODY), _request(headers)
    )
    result = _result(response)
    assert result["status"] == "failed"
    assert result["evidence"]["publication_state"] == "prior_outcome_unknown"


@pytest.mark.asyncio
async def test_strata_recipient_charges_one_post_on_the_parent_ledger(test_engine, monkeypatch):
    """With the GPU on Strata the SQL table stopped as an unsupported provider
    (live 2026-10-06); its reasoning leaf is the same one-POST boundary."""
    from tests.test_work_budget_direct_text import _OpenAIResponse

    factory, run, _parent, headers = await _setup(test_engine, monkeypatch)
    context = await resolve_sql_recipient_context(_request(headers))
    _install_local_runtime(monkeypatch, provider="strata")
    posts, _ = _install_http(monkeypatch, [_OpenAIResponse("SELECT 1")])

    with bind_http_recipient_budget_context(context):
        await context.assert_ready()
        result = await ollama_client.reasoning_generate("sql", confidential=True)

    assert result == "SELECT 1"
    rows = await _rows(factory, run["work_order_id"], "recipient-llm:")
    assert len(posts) == len(rows) == 1
    assert rows[0].state == "charged"
    assert posts[0][0].endswith("/v1/chat/completions")


@pytest.mark.asyncio
async def test_recipient_still_refuses_a_cloud_provider(test_engine, monkeypatch):
    _factory, _run, _parent, headers = await _setup(test_engine, monkeypatch)
    context = await resolve_sql_recipient_context(_request(headers))
    _install_local_runtime(monkeypatch, provider="anthropic")
    posts, _ = _install_http(monkeypatch, [])

    with bind_http_recipient_budget_context(context), pytest.raises(BudgetExecutionStopped) as stop:
        await ollama_client.reasoning_generate("sql")

    assert stop.value.code == "http_recipient_provider_unsupported"
    assert posts == []
