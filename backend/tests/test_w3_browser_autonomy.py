"""W3: reading the web is free; acting on a site takes a grant and a card.

The first reading action of a work gets the automatic read grant; a click
or typing does not. A grant that names hosts is used before the read grant,
a session action carries its grant's hosts to the browser, looking does not
spend the budget, and only the owner or a manager grants — never the agent.
"""

from __future__ import annotations

import types
import uuid

import httpx
import pytest

from app.auth.models import UserInfo, UserRole


class _Browser:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def install(self, monkeypatch):
        browser = self

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
                browser.calls.append((path, json))
                body = {"ok": True, "session_id": "s-1", "url": "https://shop.example/"}
                return httpx.Response(200, json=body, request=httpx.Request("POST", url))

        monkeypatch.setattr("app.api.computer_use.httpx.AsyncClient", _Client)


async def _order(client) -> str:
    return (await client.post("/api/work-orders", json={"objective": "Подбор"})).json()["id"]


async def _execute(client, order, action, target="s-1", **arguments):
    return await client.post(
        "/api/computer-use/execute",
        json={"action": action, "work_order_id": order, "target": target, "arguments": arguments},
    )


async def _grants(client, order) -> list[dict]:
    return (await client.get(f"/api/work-orders/{order}/computer-grants")).json()


@pytest.mark.asyncio
async def test_reading_gets_the_read_grant_and_a_click_does_not(client, monkeypatch):
    browser = _Browser()
    browser.install(monkeypatch)
    order = await _order(client)

    started = await _execute(client, order, "desktop_start", target="https://any-site.example/")
    assert started.status_code == 200, started.text
    opened = await _execute(client, order, "desktop_open", ref="1:2", revision=1)
    assert opened.status_code == 200
    assert browser.calls[-1][1]["action"] == "open"
    assert browser.calls[-1][1]["grant_hosts"] == ["*"]

    [grant] = await _grants(client, order)
    assert grant["automatic"] and grant["max_actions"] == 500

    clicked = await _execute(client, order, "desktop_click", ref="1:3", revision=1, card={})
    assert clicked.status_code == 423
    typed = await _execute(client, order, "desktop_type", ref="1:4", revision=1, text="x")
    assert typed.status_code == 423
    assert len(await _grants(client, order)) == 1


@pytest.mark.asyncio
async def test_looking_does_not_spend_the_budget(client, monkeypatch):
    _Browser().install(monkeypatch)
    order = await _order(client)
    await _execute(client, order, "desktop_start", target="https://shop.example/")
    for action in ("desktop_observe", "desktop_read", "desktop_scroll", "desktop_wait"):
        assert (await _execute(client, order, action)).status_code == 200
    await _execute(client, order, "desktop_open", ref="1:1", revision=1)
    [grant] = await _grants(client, order)
    assert grant["used_actions"] == 2  # start and open


@pytest.mark.asyncio
async def test_a_named_grant_comes_before_the_read_grant(client, monkeypatch):
    browser = _Browser()
    browser.install(monkeypatch)
    order = await _order(client)
    await _execute(client, order, "browser_fetch", target="https://other.example/")  # read grant
    named = await client.post(
        f"/api/work-orders/{order}/computer-grants",
        json={
            "actions": [
                "desktop_start",
                "desktop_observe",
                "desktop_click",
                "desktop_prepare_submit",
            ],
            "allowed_hosts": ["portal.example"],
            "max_actions": 20,
            "reason": "кабинет поставщика",
        },
    )
    assert named.status_code == 201, named.text

    await _execute(client, order, "desktop_start", target="https://portal.example/login")
    clicked = await _execute(client, order, "desktop_click", ref="2:1", revision=2, card={"a": 1})
    assert clicked.status_code == 200, clicked.text
    # The browser is told the click may only land on the portal's pages.
    assert browser.calls[-1][1]["grant_hosts"] == ["portal.example"]
    by_id = {g["id"]: g for g in await _grants(client, order)}
    assert by_id[named.json()["id"]]["used_actions"] == 2


@pytest.mark.asyncio
async def test_no_read_grant_when_switched_off(client, monkeypatch):
    from app.ai import web_search_config as wsc

    _Browser().install(monkeypatch)
    monkeypatch.setattr(
        wsc, "get_config", lambda: wsc.WebSearchConfig(browser_read_autogrant=False)
    )
    order = await _order(client)
    resp = await _execute(client, order, "desktop_start", target="https://shop.example/")
    assert resp.status_code == 423
    assert await _grants(client, order) == []


@pytest.mark.asyncio
async def test_the_owner_revokes_the_read_grant(client, monkeypatch):
    _Browser().install(monkeypatch)
    order = await _order(client)
    await _execute(client, order, "browser_fetch", target="https://shop.example/")
    [grant] = await _grants(client, order)
    revoked = await client.post(f"/api/work-orders/{order}/computer-grants/{grant['id']}/revoke")
    assert revoked.status_code == 200
    [after] = await _grants(client, order)
    assert after["active"] is False and after["revoked_at"]


def _user(sub: str, *roles: UserRole, via_agent: bool = False) -> UserInfo:
    return UserInfo(
        sub=sub, email="", name="", preferred_username="", roles=list(roles), via_agent=via_agent
    )


def test_who_may_grant():
    from app.api.work_orders import _may_grant

    order = types.SimpleNamespace(owner_key="alice", id=uuid.uuid4())
    assert _may_grant(order, _user("alice", UserRole.viewer))
    assert _may_grant(order, _user("bob", UserRole.manager))
    assert not _may_grant(order, _user("bob", UserRole.viewer))
    assert not _may_grant(order, _user("agent-service", UserRole.admin))
    # The agent acting for alice is still not alice granting.
    assert not _may_grant(order, _user("alice", UserRole.viewer, via_agent=True))
