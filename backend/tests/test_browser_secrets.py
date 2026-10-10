"""E34: brokered browser secrets — a person stores them, a grant names them,
each is typed once on its origin and its value is never returned or logged."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import ComputerUseAction

SECRET = "S3cr3t-Пароль!"


class _Browser:
    def __init__(self):
        self.payloads: list[dict] = []

    def install(self, monkeypatch):
        import httpx

        browser = self

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                browser.payloads.append(json)
                return httpx.Response(
                    200,
                    json={"ok": True, "session_id": "s-1", "url": "https://bank.example/"},
                    request=httpx.Request("POST", url),
                )

        monkeypatch.setattr("app.api.computer_use.httpx.AsyncClient", _Client)


async def _secret(client, origin="https://bank.example/login"):
    resp = await client.post(
        "/api/browser-secrets", json={"label": "Банк", "origin": origin, "value": SECRET}
    )
    assert resp.status_code == 201, resp.text
    assert SECRET not in resp.text
    return resp.json()


async def _order_with_grant(client, secret_ids):
    order = (await client.post("/api/work-orders", json={"objective": "Login"})).json()["id"]
    grant = await client.post(
        f"/api/work-orders/{order}/computer-grants",
        json={
            "actions": ["desktop_start", "desktop_fill_secret"],
            "allowed_hosts": ["bank.example"],
            "secret_ids": secret_ids,
            "max_actions": 10,
            "reason": "test",
        },
    )
    return order, grant


def _fill(order, secret_id):
    return {
        "action": "desktop_fill_secret",
        "work_order_id": order,
        "target": "s-1",
        "arguments": {"ref": "1:2", "revision": 1, "secret_id": secret_id},
    }


@pytest.mark.asyncio
async def test_a_secret_is_typed_once_and_never_comes_back(client, db_session, monkeypatch):
    browser = _Browser()
    browser.install(monkeypatch)
    secret = await _secret(client)
    assert secret["origin"] == "https://bank.example"
    listed = await client.get("/api/browser-secrets")
    assert SECRET not in listed.text and listed.json()[0]["id"] == secret["id"]

    order, grant = await _order_with_grant(client, [secret["id"]])
    assert grant.status_code == 201, grant.text

    first = await client.post("/api/computer-use/execute", json=_fill(order, secret["id"]))
    assert first.status_code == 200, first.text
    assert SECRET not in first.text
    sent = browser.payloads[-1]
    assert (sent["action"], sent["secret"], sent["origin"]) == (
        "fill_secret",
        SECRET,
        "https://bank.example",
    )

    again = await client.post("/api/computer-use/execute", json=_fill(order, secret["id"]))
    assert again.status_code == 409

    rows = (await db_session.scalars(select(ComputerUseAction))).all()
    assert rows and all(SECRET not in str(r.arguments) + str(r.result) for r in rows)


@pytest.mark.asyncio
async def test_an_ungranted_or_revoked_secret_is_not_typed(client, monkeypatch):
    _Browser().install(monkeypatch)
    granted = await _secret(client)
    other = await _secret(client, "https://other.example")
    order, grant = await _order_with_grant(client, [granted["id"]])
    assert grant.status_code == 201
    refused = await client.post("/api/computer-use/execute", json=_fill(order, other["id"]))
    assert refused.status_code == 403

    await client.post(f"/api/browser-secrets/{granted['id']}/revoke")
    revoked = await client.post("/api/computer-use/execute", json=_fill(order, granted["id"]))
    assert revoked.status_code == 403


@pytest.mark.asyncio
async def test_a_grant_cannot_name_a_revoked_secret_or_lack_the_action(client):
    secret = await _secret(client)
    order = (await client.post("/api/work-orders", json={"objective": "x"})).json()["id"]
    no_action = await client.post(
        f"/api/work-orders/{order}/computer-grants",
        json={"actions": ["desktop_start"], "secret_ids": [secret["id"]], "reason": "t"},
    )
    assert no_action.status_code == 422
    await client.post(f"/api/browser-secrets/{secret['id']}/revoke")
    _order, revoked = await _order_with_grant(client, [secret["id"]])
    assert revoked.status_code == 422


@pytest.mark.asyncio
async def test_the_agent_cannot_store_a_secret(client):
    from app.auth.acting import get_effective_user
    from app.auth.models import UserInfo, UserRole
    from app.main import app

    agent = UserInfo(
        sub="u1",
        email="u@x",
        name="u",
        preferred_username="u",
        roles=[UserRole.admin],
        via_agent=True,
    )
    app.dependency_overrides[get_effective_user] = lambda: agent
    try:
        resp = await client.post(
            "/api/browser-secrets",
            json={"label": "x", "origin": "https://a.example", "value": "v"},
        )
    finally:
        app.dependency_overrides.pop(get_effective_user, None)
    assert resp.status_code == 403
