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


@pytest.mark.asyncio
async def test_ops_signals_answer_what_is_stuck(client):
    ops = (await client.get("/api/work-orders/metrics")).json()["ops"]
    assert set(ops) >= {
        "queue_oldest_ready_age_seconds",
        "unknown_effect_calls",
        "awaiting_human_steps",
        "expired_leases",
        "failed_checks",
        "budget_blocked_ledgers",
        "outbox_pending",
        "outbox_dead_or_unknown",
    }
    assert all(v is None or v >= 0 for v in ops.values())


@pytest.mark.asyncio
async def test_usage_counts_unknown_as_unknown(client, db_session):
    """A call whose receipt does not know its tokens or price is not zero."""
    import uuid as _uuid
    from decimal import Decimal

    from app.db.models import WorkEvent, WorkOrder
    from app.db.work_budget_models import WorkBudgetReservation
    from app.domain.work_budget_usage import LLM_USAGE_EVENT_TYPE

    order = (await client.post("/api/work-orders", json={"objective": "Usage"})).json()
    row = await db_session.get(WorkOrder, _uuid.UUID(order["id"]))
    known = WorkBudgetReservation(
        ledger_id=row.budget_ledger_id,
        work_order_id=row.id,
        operation_key="llm:a",
        dimension="llm_calls",
        reserved_units=Decimal(1),
        request_digest="a" * 64,
        binding_digest="b" * 64,
        state="charged",
        actual_units=Decimal(1),
    )
    unknown = WorkBudgetReservation(
        ledger_id=row.budget_ledger_id,
        work_order_id=row.id,
        operation_key="llm:b",
        dimension="llm_calls",
        reserved_units=Decimal(1),
        request_digest="c" * 64,
        binding_digest="d" * 64,
        state="charged",
        actual_units=Decimal(1),
    )
    db_session.add_all([known, unknown])
    fence = WorkBudgetReservation(
        ledger_id=row.budget_ledger_id,
        work_order_id=row.id,
        operation_key="plan:x:once",
        dimension="llm_calls",
        reserved_units=Decimal(0),
        request_digest="e" * 64,
        binding_digest="f" * 64,
        state="reserved",
    )
    db_session.add(fence)  # an execution fence, not a call
    await db_session.flush()
    for reservation, tokens in (
        (known, {"status": "known", "units": 120}),
        (unknown, {"status": "unknown"}),
    ):
        db_session.add(
            WorkEvent(
                work_order_id=row.id,
                sequence=1000 + len(tokens),
                event_type=LLM_USAGE_EVENT_TYPE,
                actor="worker",
                payload={
                    "reservation_id": str(reservation.id),
                    "provider": "ollama",
                    "outcome": "response_observed",
                    "tokens": {"total": tokens},
                    "cost": {"status": "unknown"},
                },
            )
        )
    await db_session.commit()

    usage = (await client.get(f"/api/work-orders/{order['id']}/usage")).json()
    ollama = usage["llm_calls"]["ollama"]
    assert (ollama["calls"], ollama["tokens_known"], ollama["tokens_unknown_calls"]) == (2, 120, 1)
    assert usage["cost"] == {"status": "unknown", "usd": None}
