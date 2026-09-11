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
