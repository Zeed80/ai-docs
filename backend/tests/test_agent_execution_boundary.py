"""Regression checks for identity, effects, ownership and retired execution paths."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.ai.actor_context import get_acting_user, set_acting_user
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.policy_engine import check_tool_execution
from app.ai.tool_result import VerifierResponse, result_failed
from app.auth.execution_context import sign_execution_context, verify_execution_context
from app.auth.models import UserInfo, UserRole


@pytest.fixture(autouse=True)
def restore_actor():
    original = get_acting_user()
    yield
    set_acting_user(original)


def test_execution_context_rejects_tampering_and_expiry(monkeypatch):
    from app.auth import execution_context

    monkeypatch.setattr(execution_context.time, "time", lambda: 1000)
    token = sign_execution_context("alice")
    assert verify_execution_context(token).actor == "alice"
    with pytest.raises(HTTPException):
        verify_execution_context(token + "0")
    monkeypatch.setattr(execution_context.time, "time", lambda: 1060)
    with pytest.raises(HTTPException):
        verify_execution_context(token)


@pytest.mark.asyncio
async def test_service_requests_require_signed_actor_in_both_auth_paths(monkeypatch):
    from app.auth import jwt

    monkeypatch.setattr(jwt.settings, "auth_enabled", True)
    monkeypatch.setattr(jwt.settings, "agent_service_key", "boundary-test-key")
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-api-key", b"boundary-test-key"),
                (b"x-acting-user", b"alice"),
            ],
        }
    )
    with pytest.raises(HTTPException) as exc:
        await jwt.get_current_user(request, access_token=None)
    assert exc.value.status_code == 401
    assert await jwt.get_current_user_optional(request, access_token=None) is None


@pytest.mark.asyncio
async def test_signed_service_call_resolves_current_database_permissions(monkeypatch):
    from app.auth import execution_context, jwt

    monkeypatch.setattr(jwt.settings, "auth_enabled", True)
    monkeypatch.setattr(jwt.settings, "agent_service_key", "boundary-test-key")
    human = UserInfo(
        sub="alice",
        email="a@test.invalid",
        name="Alice",
        preferred_username="alice",
        roles=[UserRole.viewer],
        via_agent=True,
    )
    resolve = AsyncMock(return_value=human)
    monkeypatch.setattr(execution_context, "resolve_execution_actor", resolve)
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-api-key", b"boundary-test-key"),
                (b"x-acting-user", b"forged-admin"),
                (b"x-execution-context", sign_execution_context("alice").encode()),
            ],
        }
    )
    assert await jwt.get_current_user(request, access_token=None) is human
    resolve.assert_awaited_once_with("alice")
    assert get_acting_user() == "alice"
    assert jwt.is_service_account(human)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability,body",
    [
        ("agent_control", {"action": "task_create", "title": "escalate"}),
        ("documents", {"action": "delete", "document_id": "test"}),
    ],
)
async def test_viewer_cannot_use_gateway_as_admin(client, monkeypatch, capability, body):
    from app.api import capability_router
    from app.auth.jwt import get_current_user
    from app.main import app

    viewer = UserInfo(
        sub="viewer",
        email="v@test.invalid",
        name="Viewer",
        preferred_username="viewer",
        roles=[UserRole.viewer],
    )
    old = app.dependency_overrides.copy()
    app.dependency_overrides[get_current_user] = lambda: viewer
    proxy = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(capability_router, "_proxy", proxy)
    try:
        response = await client.post(
            f"/api/agent/cap/{capability}", json=body, headers={"X-Acting-User": "admin"}
        )
        assert response.status_code == 403
        proxy.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize(
    "name,args",
    [
        ("docs.update", {}),
        ("agent_control", {"action": "task_create"}),
        ("computer_use", {"action": "file_write"}),
        ("unknown.read", {}),
    ],
)
def test_read_only_uses_reviewed_effects_not_action_spelling(name, args):
    decision = check_tool_execution(
        skill_name=name,
        args=args,
        config=BuiltinAgentConfig(permission_mode="read_only"),
        approval_gates=set(),
    )
    assert not decision.allowed


def test_workspace_same_block_id_is_owner_scoped():
    from app.domain import workspace

    set_acting_user("alice-boundary")
    workspace.upsert_workspace_block("shared-id", {"text": "private alice"})
    set_acting_user("bob-boundary")
    assert workspace.get_workspace_block("shared-id") is None
    workspace.upsert_workspace_block("shared-id", {"text": "private bob"})
    workspace.clear_workspace_blocks()
    set_acting_user("alice-boundary")
    assert workspace.get_workspace_block("shared-id")["text"] == "private alice"
    workspace.clear_workspace_blocks()


@pytest.mark.asyncio
async def test_global_chat_cannot_carry_content(monkeypatch):
    import asyncio

    from app.core import chat_bus

    bus = chat_bus.ChatBus()
    alice, bob = AsyncMock(), AsyncMock()
    bus.subscribe(alice, "alice")
    bus.subscribe(bob, "bob")
    bus._dispatch_local("sveta:bus:global", {"type": "text", "content": "secret"})
    await asyncio.sleep(0)
    alice.assert_not_awaited()
    bob.assert_not_awaited()
    bus._dispatch_local("sveta:bus:user:alice", {"type": "text", "content": "secret"})
    await asyncio.sleep(0)
    alice.assert_awaited_once()
    bob.assert_not_awaited()


def test_verifier_requires_real_boolean():
    with pytest.raises(ValidationError):
        VerifierResponse.model_validate(
            {
                "verdicts": [
                    {"criterion_id": "c1", "ok": "false", "reason": "failed"},
                ]
            }
        )
    assert result_failed({"built": False})
    assert result_failed({"status": "outcome_unknown"})


def test_persisted_config_cannot_reactivate_generated_capabilities():
    config = BuiltinAgentConfig(allow_capability_builder=True, use_turn_router=False)
    assert not config.allow_capability_builder
    assert config.use_turn_router


def test_parallel_execution_requires_known_read_effects():
    from app.ai.tool_parallelism import should_parallelize

    def call(action):
        return {"function": {"name": "documents", "arguments": {"action": action}}}

    assert should_parallelize([call("list"), call("get")])
    assert not should_parallelize([call("list"), call("delete")])
    assert not should_parallelize([call("list"), call("get_and_delete")])


@pytest.mark.asyncio
async def test_vault_rejects_other_owner_and_arbitrary_redis_keys(monkeypatch):
    from app.ai.turn_vault import vault_get, vault_store

    values = {}

    async def save(key, value, **kwargs):
        values[key] = value

    redis = AsyncMock()
    redis.set.side_effect = save
    redis.get.side_effect = lambda key: values.get(key)
    monkeypatch.setattr("app.utils.redis_client.get_async_redis", lambda: redis)
    set_acting_user("alice")
    ref = await vault_store("session", {"items": [1, 2]})
    assert (await vault_get(ref))["items"] == [1, 2]
    set_acting_user("bob")
    assert await vault_get(ref) is None
    redis.get.reset_mock()
    assert await vault_get("auth:secret") is None
    redis.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_shell_is_retired_even_with_a_command_grant():
    from app.api.computer_use import _perform
    from app.db.models import ComputerUseGrant

    grant = ComputerUseGrant(allowed_commands=["cat"], allowed_roots=["/tmp"])
    with pytest.raises(HTTPException) as exc:
        await _perform("shell", "cat /etc/os-release", {}, grant)
    assert exc.value.status_code == 410


@pytest.mark.asyncio
async def test_expired_attempt_cannot_complete(db_session):
    from datetime import UTC, datetime, timedelta

    from app.domain.work_orders import (
        claim_ready_step,
        complete_attempt,
        create_single_step_plan,
        create_work_order,
    )

    order = await create_work_order(db_session, owner_key="fence-test", objective="Test lease")
    await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": "test"}
    )
    order, step, attempt = await claim_ready_step(
        db_session, worker_id="worker", work_order_id=order.id
    )
    step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ValueError, match="Stale execution"):
        await complete_attempt(
            db_session,
            order=order,
            step=step,
            attempt=attempt,
            output={"text": "late result"},
            actor="worker",
        )
    assert attempt.status == "running"
    assert step.output is None


@pytest.mark.asyncio
async def test_mutation_stops_when_audit_is_unavailable(client, monkeypatch):
    from app.api import capability_router

    monkeypatch.setattr(
        "app.audit.service.log_action", AsyncMock(side_effect=RuntimeError("DB down"))
    )
    proxy = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(capability_router, "_proxy", proxy)
    response = await client.post(
        "/api/agent/cap/image_studio",
        json={
            "action": "accept",
            "generation_id": "00000000-0000-0000-0000-000000000001",
        },
    )
    assert response.status_code == 503
    proxy.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_schema_migration_round_trip(db_session):
    import importlib.util
    import uuid
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    path = (
        Path(__file__).resolve().parents[1] / "migrations/versions/20260910_0001_agent_execution.py"
    )
    spec = importlib.util.spec_from_file_location("agent_schema_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "agent_migration_" + uuid.uuid4().hex
    connection = await db_session.connection()
    await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))

    def verify(sync_connection):
        module.op = Operations(MigrationContext.configure(sync_connection))
        module.upgrade()
        assert set(inspect(sync_connection).get_table_names(schema=schema)) == {
            "owned_workspace_blocks",
            "agent_delegation_grants",
            "agent_channel_identities",
            "agent_script_runs",
        }
        module.downgrade()
        assert not inspect(sync_connection).get_table_names(schema=schema)

    await connection.run_sync(verify)
