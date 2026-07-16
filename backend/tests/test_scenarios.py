"""Tests for Scenarios API — list and trigger AiAgent scenarios."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_scenarios(client: AsyncClient):
    resp = await client.get("/api/scenarios")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_run_scenario_not_found(client: AsyncClient):
    resp = await client.post("/api/scenarios/nonexistent-scenario-xyz/run", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reload_agent_config(client: AsyncClient):
    resp = await client.post("/api/scenarios/agent/reload-config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "reloaded"
    assert "exposed_skills" in data
    assert "approval_gates" in data


@pytest.mark.asyncio
async def test_dry_run_cad_digitize_release(client: AsyncClient):
    """G1: the dry-run plans the full chain without executing anything —
    every referenced skill resolves, approval-gated steps are absent (the
    agent stops BEFORE accept/approve), and templates are rendered from the
    trigger."""
    resp = await client.post(
        "/api/scenarios/cad_digitize_release/dry-run",
        json={"trigger": {"document_id": "doc-1", "project_name": "Вал"}},
    )
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["executable"] is True
    assert plan["missing_skills"] == []
    step_ids = [s["step_id"] for s in plan["steps"]]
    assert step_ids == [
        "digitize", "wait_done", "review_summary", "full_check",
        "create_project", "draft_revision", "link_cad", "validate_release",
    ]
    digitize = plan["steps"][0]
    assert digitize["skill"] == "image_studio"
    assert digitize["params"]["source_document_ids"] == ["doc-1"]
    # draft-first: no step in the chain presses an approval-gated action …
    assert plan["approval_gated_steps"] == []
    # … but the gates themselves are declared for the human
    assert "image_studio.accept_vectorize" in plan["declared_gates"]
    assert "engineering.revision_approve" in plan["declared_gates"]
    # the wait step carries the polling contract
    wait = plan["steps"][1]
    assert wait["until"] == "last.status == 'done'"


@pytest.mark.asyncio
async def test_dry_run_unknown_scenario(client: AsyncClient):
    resp = await client.post("/api/scenarios/nope-xyz/dry-run", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_step_polling_until_condition(monkeypatch):
    """The `until` step repeats the skill call until the condition holds on
    the last result."""
    from app.ai import scenario_runner as sr

    calls = {"n": 0}

    async def fake_skill(name, params):
        calls["n"] += 1
        return {"status": "done" if calls["n"] >= 3 else "running"}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(sr, "_call_skill", fake_skill)
    monkeypatch.setattr("asyncio.sleep", no_sleep)
    ctx = sr._Context({})
    result = await sr._run_step(
        {"id": "wait", "skill": "image_studio",
         "params": {"action": "get"}, "until": "last.status == 'done'",
         "poll_interval_s": 0, "max_polls": 10},
        ctx,
    )
    assert result["status"] == "done"
    assert result["_polls"] == 3
