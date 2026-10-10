"""ComputerUseGrant enforcement at the /api/computer-use/execute broker —
short-lived, least-privilege grants scoped to a WorkOrder; no grant, no
action, and every allowed action is audited.

Split out of test_work_orders.py (Б18) — see that file's docstring for the
full split map.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_computer_use_broker_enforces_grant_and_audits_file_action(client, tmp_path):
    created = await client.post("/api/work-orders", json={"objective": "Write broker file"})
    assert created.status_code == 201
    order_id = created.json()["id"]
    target = tmp_path / "result.txt"
    denied = await client.post(
        "/api/computer-use/execute",
        json={
            "action": "file_write",
            "work_order_id": order_id,
            "target": str(target),
            "arguments": {"content": "verified"},
        },
    )
    assert denied.status_code == 423
    granted = await client.post(
        f"/api/work-orders/{order_id}/computer-grants",
        json={
            "actions": ["file_write", "file_read"],
            "allowed_roots": [str(tmp_path)],
            "max_actions": 2,
            "reason": "test",
        },
    )
    assert granted.status_code == 201, granted.text
    written = await client.post(
        "/api/computer-use/execute",
        json={
            "action": "file_write",
            "work_order_id": order_id,
            "target": str(target),
            "arguments": {"content": "verified"},
        },
    )
    assert written.status_code == 200, written.text
    assert written.json()["result"]["sha256"]
    read = await client.post(
        "/api/computer-use/execute",
        json={"action": "file_read", "work_order_id": order_id, "target": str(target)},
    )
    assert read.status_code == 200, read.text
    assert read.json()["result"]["content"] == "verified"


async def _granted_order(client, tmp_path):
    created = await client.post("/api/work-orders", json={"objective": "Revocable broker file"})
    order_id = created.json()["id"]
    granted = await client.post(
        f"/api/work-orders/{order_id}/computer-grants",
        json={
            "actions": ["file_write"],
            "allowed_roots": [str(tmp_path)],
            "max_actions": 5,
            "reason": "test",
        },
    )
    assert granted.status_code == 201, granted.text
    return order_id, granted.json()["id"]


def _write(order_id, target):
    return {
        "action": "file_write",
        "work_order_id": order_id,
        "target": str(target),
        "arguments": {"content": "x"},
    }


@pytest.mark.asyncio
async def test_a_revoked_grant_authorizes_nothing_more(client, tmp_path):
    """E24: a grant could not be taken back before its TTL or action limit."""
    order_id, grant_id = await _granted_order(client, tmp_path)
    target = tmp_path / "a.txt"
    assert (
        await client.post("/api/computer-use/execute", json=_write(order_id, target))
    ).status_code == 200

    revoked = await client.post(f"/api/work-orders/{order_id}/computer-grants/{grant_id}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"]

    again = await client.post(
        "/api/computer-use/execute", json=_write(order_id, tmp_path / "b.txt")
    )
    assert again.status_code == 423
    assert not (tmp_path / "b.txt").exists()


@pytest.mark.asyncio
async def test_canceling_the_order_revokes_its_grants(client, tmp_path):
    order_id, _grant_id = await _granted_order(client, tmp_path)
    canceled = await client.post(f"/api/work-orders/{order_id}/cancel")
    assert canceled.status_code == 200, canceled.text

    after = await client.post(
        "/api/computer-use/execute", json=_write(order_id, tmp_path / "c.txt")
    )
    assert after.status_code == 423
    assert not (tmp_path / "c.txt").exists()


@pytest.mark.asyncio
async def test_a_child_order_does_not_inherit_the_parents_grant(client, db_session, tmp_path):
    """E24 card: child execution gets a bounded context. Grants are bound to
    one work order, so a decomposed child acts without the parent's."""
    import uuid

    from app.db.models import WorkOrder
    from app.domain.work_orders import create_work_order

    parent_id, _grant_id = await _granted_order(client, tmp_path)
    parent = await db_session.get(WorkOrder, uuid.UUID(parent_id))
    child = await create_work_order(
        db_session,
        owner_key=parent.owner_key,
        objective="Child of a granted order",
        parent_id=parent.id,
    )
    await db_session.flush()

    from_child = await client.post(
        "/api/computer-use/execute", json=_write(str(child.id), tmp_path / "child.txt")
    )
    assert from_child.status_code == 423
    assert not (tmp_path / "child.txt").exists()


class _BrowserService:
    """Records what the backend sends to the browser service."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def install(self, monkeypatch):
        import httpx

        service = self

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                service.calls.append((url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1], json))
                body = {"ok": True, "session_id": "s-1", "url": "https://example.com/"}
                if url.endswith("close-owner"):
                    body = {"ok": True, "closed": 1}
                return httpx.Response(200, json=body, request=httpx.Request("POST", url))

        monkeypatch.setattr("app.api.computer_use.httpx.AsyncClient", _Client)


@pytest.mark.asyncio
async def test_desktop_actions_carry_the_work_order_as_owner(client, monkeypatch):
    """E31: the browser service refuses a session id under another owner."""
    service = _BrowserService()
    service.install(monkeypatch)
    order_id = (await client.post("/api/work-orders", json={"objective": "Browse"})).json()["id"]
    granted = await client.post(
        f"/api/work-orders/{order_id}/computer-grants",
        json={
            "actions": ["desktop_start", "desktop_read"],
            "allowed_hosts": ["example.com"],
            "max_actions": 5,
            "reason": "test",
        },
    )
    assert granted.status_code == 201, granted.text
    for action, target in (("desktop_start", "https://example.com/"), ("desktop_read", "s-1")):
        resp = await client.post(
            "/api/computer-use/execute",
            json={"action": action, "work_order_id": order_id, "target": target},
        )
        assert resp.status_code == 200, resp.text
    owners = {payload["owner"] for _path, payload in service.calls}
    assert owners == {f"wo:{order_id}"}

    revoked = await client.post(
        f"/api/work-orders/{order_id}/computer-grants/{granted.json()['id']}/revoke"
    )
    assert revoked.status_code == 200
    assert service.calls[-1] == ("desktop/close-owner", {"owner": f"wo:{order_id}"})


@pytest.mark.asyncio
async def test_a_click_needs_the_card_it_was_approved_for(client, monkeypatch):
    """E36: no card, no click; the card goes to the browser for the re-check."""
    service = _BrowserService()
    service.install(monkeypatch)
    order_id = (await client.post("/api/work-orders", json={"objective": "Order"})).json()["id"]
    await client.post(
        f"/api/work-orders/{order_id}/computer-grants",
        json={
            "actions": ["desktop_prepare_submit", "desktop_click"],
            "allowed_hosts": ["example.com"],
            "reason": "test",
        },
    )

    def execute(action, body):
        return client.post(
            "/api/computer-use/execute",
            json={"action": action, "work_order_id": order_id, "target": "s-1", "arguments": body},
        )

    assert (
        await execute("desktop_prepare_submit", {"ref": "1:3", "revision": 1})
    ).status_code == 200
    assert service.calls[-1][1]["action"] == "describe"
    assert (await execute("desktop_click", {"ref": "1:3", "revision": 1})).status_code == 422
    card = {"origin": "https://example.com", "card_hash": "x"}
    assert (
        await execute("desktop_click", {"ref": "1:3", "revision": 1, "card": card})
    ).status_code == 200
    assert service.calls[-1][1]["card"] == card
