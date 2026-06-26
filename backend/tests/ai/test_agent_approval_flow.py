from __future__ import annotations

import asyncio

import pytest

from app.ai import agent_loop


@pytest.mark.asyncio
async def test_agent_approval_decision_requires_matching_id():
    sent: list[dict] = []

    async def collect(message: dict) -> None:
        sent.append(message)

    session = agent_loop.AgentSession(collect)
    session._pending_approval_id = "approval-active"
    session._approval_future = asyncio.get_running_loop().create_future()

    await session.on_approval(True, approval_id="approval-stale")

    assert session._approval_future.done() is False
    assert sent[-1]["type"] == "approval_ignored"

    await session.on_approval(True, approval_id="approval-active")

    assert session._approval_future.done() is True
    assert session._approval_future.result() is True


@pytest.mark.asyncio
async def test_gated_action_blocks_when_durable_approval_is_missing(monkeypatch):
    sent: list[dict] = []

    async def collect(message: dict) -> None:
        sent.append(message)

    async def no_db_approval(skill_name: str, args: dict) -> str | None:
        return None

    monkeypatch.setattr(agent_loop, "_create_db_approval", no_db_approval)

    session = agent_loop.AgentSession(collect)
    approved = await session._request_approval("invoice.approve", {"invoice_id": "bad-id"})

    assert approved is False
    assert sent[-1]["type"] == "approval_error"
    assert "fail-closed" in sent[-1]["message"]


def test_capability_approval_action_type_maps_broad_tool_names():
    assert (
        agent_loop._approval_action_type_for("invoices", {"action": "approve"})
        == "invoice.approve"
    )
    assert (
        agent_loop._approval_action_type_for("analytics", {"action": "compare_decide"})
        == "compare.decide"
    )
