"""E45: the server says what an order's owner can do and how much it used."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_actions_follow_the_order_state(client):
    created = await client.post("/api/work-orders", json={"objective": "View"})
    order = created.json()
    assert order["display_status"] == order["status"]
    assert "cancel" in order["available_actions"] and "pause" in order["available_actions"]

    paused = (await client.post(f"/api/work-orders/{order['id']}/pause")).json()
    assert paused["display_status"] in {"paused", "pause_requested"}
    assert "unpause" in paused["available_actions"]
    assert "pause" not in paused["available_actions"] and "run" not in paused["available_actions"]

    canceled = (await client.post(f"/api/work-orders/{order['id']}/cancel")).json()
    assert canceled["available_actions"] == []
    listed = (await client.get("/api/work-orders?limit=5")).json()
    assert all("available_actions" in row for row in listed)


@pytest.mark.asyncio
async def test_budget_shows_limits_and_use(client):
    order = (await client.post("/api/work-orders", json={"objective": "Budget"})).json()
    budget = (await client.get(f"/api/work-orders/{order['id']}/budget")).json()
    assert budget["ledger"]
    dims = budget["dimensions"]
    assert dims["tool_attempts"]["limit"] == 200.0 and dims["tool_attempts"]["used"] == 0.0
    assert set(dims) == {
        "active_seconds",
        "tool_attempts",
        "llm_calls",
        "replans",
        "tokens",
        "cost_usd",
    }
