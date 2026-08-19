"""Б14: GET /api/work-orders/metrics — status counts + p50/p95 step duration
over a time window, scoped to the caller unless admin.
"""

from __future__ import annotations

import pytest

from app.domain.work_orders import (
    claim_ready_step,
    complete_attempt,
    create_single_step_plan,
    create_work_order,
)


@pytest.mark.asyncio
async def test_metrics_counts_status_and_computes_duration_percentiles(client, db_session):
    order = await create_work_order(db_session, owner_key="tester", objective="Metrics me")
    await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Execute", input_data={"prompt": "x"},
    )
    claimed = await claim_ready_step(db_session, worker_id="w", work_order_id=order.id)
    assert claimed is not None
    c_order, c_step, c_attempt = claimed
    await complete_attempt(
        db_session, order=c_order, step=c_step, attempt=c_attempt,
        output={"text": "готово"}, actor="w",
    )
    await db_session.commit()

    response = await client.get("/api/work-orders/metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["window_hours"] == 24
    assert sum(body["status_counts"].values()) >= 1
    # The one attempt above completed almost instantly in this test, so it
    # should show up as a single-sample group with p50 == p95.
    matching = [d for k, d in body["step_durations"].items() if k.startswith("agent_turn")]
    assert matching, body["step_durations"]
    assert matching[0]["count"] >= 1
    assert matching[0]["p50_seconds"] == matching[0]["p95_seconds"]


@pytest.mark.asyncio
async def test_metrics_accepts_custom_window(client):
    response = await client.get("/api/work-orders/metrics?hours=1")
    assert response.status_code == 200
    assert response.json()["window_hours"] == 1


@pytest.mark.asyncio
async def test_metrics_rejects_window_over_thirty_days(client):
    response = await client.get("/api/work-orders/metrics?hours=999999")
    assert response.status_code == 422
