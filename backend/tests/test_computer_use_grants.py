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
