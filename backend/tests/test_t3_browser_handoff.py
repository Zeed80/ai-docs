"""T3: the agent hands a browser session to its owner for MFA or CAPTCHA.

The owner — a person, never the agent — sees the session and acts in it; each
input is journaled as the owner's without what was typed.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.db.models import ComputerUseAction

CODE = "482913"


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
                path = url.split("://", 1)[-1].split("/", 1)[-1]
                browser.calls.append(("/" + path, json))
                if path.endswith("list-owner"):
                    body = {"ok": True, "sessions": [{"session_id": "s-1", "handoff": None}]}
                else:
                    body = {
                        "ok": True,
                        "session_id": "s-1",
                        "handed_off": True,
                        "url": "https://bank.example/mfa",
                        "handoff": {"reason": "SMS"},
                    }
                return httpx.Response(200, json=body, request=httpx.Request("POST", url))

        monkeypatch.setattr("app.api.computer_use.httpx.AsyncClient", _Client)


async def _order(client, actions=("desktop_start", "desktop_observe")):
    order = (await client.post("/api/work-orders", json={"objective": "Login"})).json()["id"]
    grant = await client.post(
        f"/api/work-orders/{order}/computer-grants",
        json={
            "actions": list(actions),
            "allowed_hosts": ["bank.example"],
            "max_actions": 10,
            "reason": "test",
        },
    )
    assert grant.status_code == 201, grant.text
    return order


@pytest.mark.asyncio
async def test_a_started_session_can_be_handed_to_the_owner(client, monkeypatch):
    browser = _Browser()
    browser.install(monkeypatch)
    order = await _order(client)
    resp = await client.post(
        "/api/computer-use/execute",
        json={
            "action": "desktop_handoff",
            "work_order_id": order,
            "target": "s-1",
            "arguments": {"reason": "Нужен код из SMS"},
        },
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["owner_url"] == f"/work-orders/browser?work_order_id={order}&session=s-1"
    sent = browser.calls[-1][1]
    assert sent["action"] == "handoff" and sent["reason"] == "Нужен код из SMS"


@pytest.mark.asyncio
async def test_without_a_session_grant_there_is_no_handoff(client, monkeypatch):
    _Browser().install(monkeypatch)
    order = await _order(client, actions=("browser_fetch",))
    resp = await client.post(
        "/api/computer-use/execute",
        json={"action": "desktop_handoff", "work_order_id": order, "target": "s-1"},
    )
    assert resp.status_code == 423


@pytest.mark.asyncio
async def test_the_owners_input_is_journaled_without_what_was_typed(
    client, db_session, monkeypatch
):
    browser = _Browser()
    browser.install(monkeypatch)
    order = await _order(client)
    listed = await client.get("/api/computer-use/sessions", params={"work_order_id": order})
    assert listed.status_code == 200 and listed.json()["sessions"][0]["session_id"] == "s-1"
    assert browser.calls[-1][1]["owner"] == f"wo:{order}"

    typed = await client.post(
        "/api/computer-use/sessions/s-1/human",
        json={"work_order_id": order, "op": "type", "text": CODE},
    )
    assert typed.status_code == 200, typed.text
    assert browser.calls[-1][1]["text"] == CODE  # the browser gets it, the journal not
    rows = list(
        await db_session.scalars(select(ComputerUseAction).where(ComputerUseAction.target == "s-1"))
    )
    mine = [r for r in rows if str(r.work_order_id) == order]
    assert [r.action for r in mine] == ["owner_type"]
    assert mine[0].arguments == {"by": "owner", "chars": 6}
    assert CODE not in str([r.arguments, r.result, r.error] for r in mine)


@pytest.mark.asyncio
async def test_the_agent_cannot_act_as_the_owner(client, monkeypatch):
    from app.auth.acting import get_effective_user
    from app.auth.models import UserInfo, UserRole
    from app.main import app

    _Browser().install(monkeypatch)
    order = await _order(client)
    me = (await client.get("/api/auth/me")).json()
    agent = UserInfo(
        sub=me["sub"],
        email="u@x",
        name="u",
        preferred_username="u",
        roles=[UserRole.admin],
        via_agent=True,
    )
    app.dependency_overrides[get_effective_user] = lambda: agent
    try:
        resp = await client.post(
            "/api/computer-use/sessions/s-1/human",
            json={"work_order_id": order, "op": "type", "text": CODE},
        )
    finally:
        app.dependency_overrides.pop(get_effective_user, None)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_another_users_order_is_not_found(client, monkeypatch):
    from app.auth.acting import get_effective_user
    from app.auth.models import UserInfo, UserRole
    from app.main import app

    _Browser().install(monkeypatch)
    order = await _order(client)
    stranger = UserInfo(
        sub="someone-else", email="s@x", name="s", preferred_username="s", roles=[UserRole.admin]
    )
    app.dependency_overrides[get_effective_user] = lambda: stranger
    try:
        resp = await client.get("/api/computer-use/sessions", params={"work_order_id": order})
    finally:
        app.dependency_overrides.pop(get_effective_user, None)
    assert resp.status_code == 404
