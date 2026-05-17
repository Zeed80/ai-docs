from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.skip(reason="/api/agent/tools and /api/agent/scenarios endpoints not yet implemented")


@pytest.mark.asyncio
async def test_agent_tools_are_allowlisted(client: AsyncClient) -> None:
    response = await client.get("/api/agent/tools")

    assert response.status_code == 200
    tools = {tool["name"]: tool for tool in response.json()}
    assert "document.process" in tools
    assert tools["email.send.request_approval"]["approval_required"] is True


@pytest.mark.asyncio
async def test_smart_ingest_denies_unknown_tools_and_enforces_step_limit(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "Agent ingest"})
    case = case_response.json()
    response = await client.post(
        "/api/agent/scenarios/smart_ingest/run",
        json={
            "case_id": case["id"],
            "document_id": "doc-1",
            "requested_tools": [
                "document.process",
                "unknown.tool",
                "document.invoice_extraction",
                "document.drawing_analysis",
                "document.process",
                "document.process",
                "document.process",
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["max_steps"] == 6
    assert len(payload["actions"]) == 6
    assert any(action["status"] == "denied_unknown_tool" for action in payload["actions"])
    assert payload["warnings"][0].startswith("Requested 7 steps")

    audit_response = await client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "agent_scenario_started" in event_types
    assert "agent_action_recorded" in event_types
    assert "agent_scenario_completed" in event_types


@pytest.mark.asyncio
async def test_agent_scenario_creates_approval_gate_for_external_actions(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "Agent approval"})
    case = case_response.json()
    response = await client.post(
        "/api/agent/scenarios/draft_email/run",
        json={
            "case_id": case["id"],
            "draft_id": "draft-1",
            "requested_tools": ["email.draft", "email.send.request_approval"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed_with_gates"
    assert payload["approval_gates"][0]["gate_type"] == "email.send.request_approval"
    assert payload["actions"][1]["status"] == "blocked_for_approval"

    audit_response = await client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "approval_gate_created" in event_types
    assert "agent_scenario_completed" in event_types
