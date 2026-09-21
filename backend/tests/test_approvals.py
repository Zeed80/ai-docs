"""Approval API tests — approval.request, approval.status, approval.list_pending"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select


async def _recorded_recipient_approval(db_session):
    from app.ai.chat_checkpoint import pack_checkpoint
    from app.db.agent_runtime_models import ChatLogicalAction
    from app.db.models import Approval, ApprovalActionType
    from app.domain.chat_action_journal import digest
    from app.domain.work_orders import (
        claim_ready_step,
        create_work_order,
        create_work_plan,
        recorded_recipient_action_digest,
        transition_step,
        transition_work_order,
    )

    order = await create_work_order(
        db_session,
        owner_key="dev-user",
        objective="Recorded recipient decision must not replay",
        source="durable_chat",
    )
    plan, steps = await create_work_plan(
        db_session,
        order,
        steps=[{"step_key": "chat", "title": "chat", "kind": "agent_turn", "input": {}}],
    )
    await db_session.commit()
    order, step, attempt = await claim_ready_step(
        db_session, worker_id="approval-test", work_order_id=order.id
    )
    request = {"name": "agent__mcp", "arguments": {"action": "send", "draft_id": "d-1"}}
    result = {"version": 1, "status": "waiting_approval"}
    action = ChatLogicalAction(
        work_order_id=order.id,
        attempt_id=attempt.id,
        call_id="call-1",
        request=request,
        request_digest=digest(request),
        status="waiting_approval",
        result=result,
        result_digest=digest(result),
    )
    db_session.add(action)
    await db_session.flush()
    snapshot = pack_checkpoint(
        {
            "phase": "tool_recorded",
            "messages": [{"role": "tool", "tool_call_id": "call-1", "content": "recorded"}],
            "pending_calls": [],
            "in_flight_call_id": None,
            "action_ids": {"call-1": str(action.id)},
            "completed_call": {
                "action_id": str(action.id),
                "call_id": "call-1",
                "result": result,
            },
        }
    )
    attempt.status = "waiting_approval"
    attempt.output = result
    attempt.checkpoint = {
        "kind": "durable_chat",
        "owner_key": order.owner_key,
        "work_order_id": str(order.id),
        "step_id": str(step.id),
        "attempt_id": str(attempt.id),
        "plan_id": str(plan.id),
        "plan_revision": order.plan_revision,
        "snapshot": snapshot,
    }
    step.output = {"result": result, "executor": "durable_chat"}
    await transition_step(db_session, step, "waiting_approval", actor="test")
    await transition_work_order(db_session, order, "waiting_approval", actor="test")
    args = request["arguments"]
    approval = Approval(
        action_type=ApprovalActionType.agent_tool_call,
        entity_type="work_order",
        entity_id=order.id,
        requested_by="dev-user",
        context={
            "continuation_mode": "recorded_recipient_result",
            "work_order_id": str(order.id),
            "source_step_id": str(step.id),
            "source_attempt_id": str(attempt.id),
            "source_plan_id": str(plan.id),
            "source_plan_revision": order.plan_revision,
            "snapshot_sha256": snapshot["sha256"],
            "action_id": str(action.id),
            "call_id": "call-1",
            "tool_name": request["name"],
            "tool_args": args,
            "request_digest": digest(request),
            "result_digest": digest(result),
            "action_digest": recorded_recipient_action_digest(request["name"], "send", args),
            "tool_result": result,
        },
    )
    db_session.add(approval)
    await db_session.commit()
    return order.id, step.id, attempt.id, approval.id


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_recorded_recipient_decision_blocks_source_without_replay(
    client: AsyncClient, db_session, decision
):
    from app.db.models import WorkOrder, WorkStep, WorkStepAttempt

    order_id, step_id, attempt_id, approval_id = await _recorded_recipient_approval(db_session)

    deliver = AsyncMock()
    execute = AsyncMock()
    with (
        patch("app.ai.agent_loop.deliver_external_approval", new=deliver),
        patch("app.api.approvals._execute_approved_action", new=execute),
    ):
        response = await client.post(
            f"/api/approvals/{approval_id}/decide", json={"status": decision}
        )
    assert response.status_code == 200, response.text
    deliver.assert_not_awaited()
    execute.assert_not_awaited()
    db_session.expire_all()
    order = await db_session.get(WorkOrder, order_id)
    step = await db_session.get(WorkStep, step_id)
    attempt = await db_session.get(WorkStepAttempt, attempt_id)
    assert order.status == "blocked" and step.state == "failed"
    assert attempt.status == "waiting_approval"
    assert "approval" not in (step.input_ or {})
    assert not await db_session.scalar(
        select(WorkStep.id).where(WorkStep.work_order_id == order.id, WorkStep.state == "ready")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_bulk_recorded_recipient_decision_uses_same_safe_settlement(
    client: AsyncClient, db_session, decision
):
    from app.db.models import Approval, WorkOrder, WorkStep

    order_id, step_id, _attempt_id, approval_id = await _recorded_recipient_approval(db_session)
    response = await client.post(
        "/api/approvals/bulk-decide",
        json={"approval_ids": [str(approval_id)], "status": decision},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"processed": 1, "failed": 0}
    db_session.expire_all()
    assert (await db_session.get(Approval, approval_id)).status.value == decision
    assert (await db_session.get(WorkOrder, order_id)).status == "blocked"
    assert (await db_session.get(WorkStep, step_id)).state == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "delegated", "expired"])
async def test_decide_rejects_non_decision_status(client: AsyncClient, status):
    response = await client.post(
        "/api/approvals/00000000-0000-0000-0000-000000000001/decide",
        json={"status": status},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["approval_context", "snapshot"])
async def test_recorded_recipient_decision_revalidates_exact_binding(
    client: AsyncClient, db_session, tamper
):
    from app.db.models import Approval, WorkOrder, WorkStepAttempt

    order_id, _step_id, attempt_id, approval_id = await _recorded_recipient_approval(db_session)
    if tamper == "approval_context":
        approval = await db_session.get(Approval, approval_id)
        approval.context = {**approval.context, "result_digest": "forged"}
    else:
        attempt = await db_session.get(WorkStepAttempt, attempt_id)
        checkpoint = dict(attempt.checkpoint)
        checkpoint["snapshot"] = {**checkpoint["snapshot"], "sha256": "forged"}
        attempt.checkpoint = checkpoint
    await db_session.commit()

    response = await client.post(
        f"/api/approvals/{approval_id}/decide", json={"status": "approved"}
    )
    assert response.status_code == 409, response.text
    db_session.expire_all()
    assert (await db_session.get(Approval, approval_id)).status.value == "pending"
    assert (await db_session.get(WorkOrder, order_id)).status == "waiting_approval"


@pytest.mark.asyncio
async def test_create_approval(client: AsyncClient):
    """approval.request — create a pending approval."""
    resp = await client.post(
        "/api/approvals",
        json={
            "action_type": "invoice.approve",
            "entity_type": "invoice",
            "entity_id": "00000000-0000-0000-0000-000000000001",
            "requested_by": "sveta",
            "context": {"invoice_number": "INV-001", "total": 15000},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["action_type"] == "invoice.approve"
    assert data["requested_by"] == "sveta"


@pytest.mark.asyncio
async def test_get_approval(client: AsyncClient):
    """approval.status — get approval by ID."""
    create = await client.post(
        "/api/approvals",
        json={
            "action_type": "email.send",
            "entity_type": "email_draft",
            "entity_id": "00000000-0000-0000-0000-000000000002",
        },
    )
    approval_id = create.json()["id"]

    resp = await client.get(f"/api/approvals/{approval_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == approval_id


@pytest.mark.asyncio
async def test_list_pending(client: AsyncClient):
    """approval.list_pending — returns pending approvals."""
    # Use entity_type that is not subject to the orphan-exists filter
    # (filter only applies to 'document' and 'invoice' entity types)
    await client.post(
        "/api/approvals",
        json={
            "action_type": "email.send",
            "entity_type": "email_draft",
            "entity_id": "00000000-0000-0000-0000-000000000003",
        },
    )

    resp = await client.get("/api/approvals/pending")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_decide_approval(client: AsyncClient):
    """Decide on approval — approve it. decided_by must come from the auth session."""
    create = await client.post(
        "/api/approvals",
        json={
            "action_type": "invoice.approve",
            "entity_type": "invoice",
            "entity_id": "00000000-0000-0000-0000-000000000004",
        },
    )
    approval_id = create.json()["id"]

    resp = await client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"status": "approved", "comment": "Looks good"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    # decided_by must reflect the authenticated user (dev-user in tests), not a client-supplied value
    assert data["decided_by"] == "dev-user"


@pytest.mark.asyncio
async def test_decide_already_decided(client: AsyncClient):
    """Cannot decide on already decided approval."""
    create = await client.post(
        "/api/approvals",
        json={
            "action_type": "invoice.approve",
            "entity_type": "invoice",
            "entity_id": "00000000-0000-0000-0000-000000000005",
        },
    )
    approval_id = create.json()["id"]

    await client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"status": "approved"},
    )

    resp = await client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"status": "rejected"},
    )
    assert resp.status_code == 400
