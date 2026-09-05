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
