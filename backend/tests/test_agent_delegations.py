"""Standing authority is bounded, revocable and cannot elevate the agent."""

from unittest.mock import AsyncMock

import pytest

from app.domain.delegations import arguments_match, matching_delegation


async def create_grant(client, **overrides):
    response = await client.post(
        "/api/agent/delegations",
        json={
            "title": "Один согласованный счёт",
            "actions": ["invoices.approve"],
            "constraints": {"invoice_id": "00000000-0000-0000-0000-000000000001"},
            "max_actions": 1,
            "duration_hours": 2,
            **overrides,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_constraints_are_typed_exact_values():
    assert arguments_match({"invoice_id": "one"}, {"body": {"invoice_id": "one"}})
    assert not arguments_match({"invoice_id": "one"}, {"invoice_id": "two"})
    assert not arguments_match({"enabled": True}, {"enabled": 1})
    assert not arguments_match({}, {"invoice_id": "one"})


@pytest.mark.asyncio
async def test_gateway_consumes_budget_before_effect(client, monkeypatch):
    from app.api import capability_router

    await create_grant(client)
    proxy = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(capability_router, "_proxy", proxy)
    body = {"action": "approve", "invoice_id": "00000000-0000-0000-0000-000000000001"}
    first = await client.post("/api/agent/cap/invoices", json=body)
    second = await client.post("/api/agent/cap/invoices", json=body)
    assert first.status_code == 200, first.text
    assert second.status_code == 423
    proxy.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_and_argument_scope(client):
    grant = await create_grant(client)
    action = "invoices.approve"
    assert await matching_delegation("other-owner", action, grant["constraints"]) is None
    assert await matching_delegation("dev-user", action, {"invoice_id": "other"}) is None
    assert await matching_delegation("dev-user", action, grant["constraints"]) is not None
    response = await client.delete(f"/api/agent/delegations/{grant['id']}")
    assert response.status_code == 200
    assert await matching_delegation("dev-user", action, grant["constraints"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actions,constraints",
    [
        (["agent_control.task_create"], {"title": "escalate"}),
        (["computer_use.shell"], {"target": "python"}),
        (["invoices.approve"], {}),
        (["unknown.write"], {"id": "one"}),
    ],
)
async def test_unbounded_or_privileged_grants_are_rejected(client, actions, constraints):
    response = await client.post(
        "/api/agent/delegations",
        json={
            "title": "Not permitted",
            "actions": actions,
            "constraints": constraints,
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_agent_cannot_grant_itself_authority(client):
    from app.auth.jwt import _DEV_USER, get_current_user
    from app.main import app

    original = app.dependency_overrides.copy()
    app.dependency_overrides[get_current_user] = lambda: _DEV_USER.model_copy(
        update={"via_agent": True}
    )
    try:
        response = await client.post(
            "/api/agent/delegations",
            json={
                "title": "Self grant",
                "actions": ["invoices.approve"],
                "constraints": {"invoice_id": "one"},
            },
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


# ── E46: typed scope, checked by the server ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "constraints,why",
    [
        ({"supplier_id": "00000000-0000-0000-0000-000000000001"}, "не передаётся"),
        ({"invoice_id": "not-a-uuid"}, "ожидается"),
        ({"invoice_id": 42}, "ожидается"),
        ({"invoice_id": "*"}, "подстановочное"),
        ({"invoice_id": " "}, "ожидается"),
        ({"invoice_id": ["00000000-0000-0000-0000-000000000001"]}, "ожидается"),
    ],
)
async def test_a_scope_must_be_a_known_typed_exact_value(client, constraints, why):
    response = await client.post(
        "/api/agent/delegations",
        json={"title": "x", "actions": ["invoices.approve"], "constraints": constraints},
    )
    assert response.status_code == 422
    assert why in response.text


@pytest.mark.asyncio
async def test_the_form_gets_typed_fields_for_each_action(client):
    items = (await client.get("/api/agent/delegations/actions")).json()["items"]
    approve = next(i for i in items if i["name"] == "invoices.approve")
    field = next(f for f in approve["fields"] if f["name"] == "invoice_id")
    assert (field["type"], field["format"], field["required"]) == ("string", "uuid", True)
    assert all(i["fields"] for i in items)  # nothing offered that cannot be scoped


@pytest.mark.asyncio
async def test_an_expired_grant_authorizes_nothing(client, db_session):
    from datetime import UTC, datetime, timedelta

    from app.db.agent_runtime_models import DelegationGrant

    grant = await create_grant(client)
    row = await db_session.get(DelegationGrant, __import__("uuid").UUID(grant["id"]))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert await matching_delegation("dev-user", "invoices.approve", grant["constraints"]) is None


@pytest.mark.asyncio
async def test_two_concurrent_last_attempts_get_one_use(test_engine, monkeypatch):
    """The grant row is locked while a use is counted: one wins, one is refused."""
    import asyncio
    import uuid
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.db.agent_runtime_models import DelegationGrant

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    owner = f"race-{uuid.uuid4().hex[:6]}"
    constraints = {"invoice_id": "00000000-0000-0000-0000-000000000009"}
    async with factory() as db:
        grant = DelegationGrant(
            owner_key=owner,
            title="last one",
            actions=["invoices.approve"],
            constraints=constraints,
            max_actions=1,
            used_actions=0,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(grant)
        await db.commit()
        grant_id = grant.id
    try:
        results = await asyncio.gather(
            *[
                matching_delegation(owner, "invoices.approve", constraints, consume=True)
                for _ in range(2)
            ]
        )
        assert sorted(r is not None for r in results) == [False, True]
    finally:
        async with factory() as db:
            row = await db.get(DelegationGrant, grant_id)
            await db.delete(row)
            await db.commit()
