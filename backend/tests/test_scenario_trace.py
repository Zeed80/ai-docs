"""Tests for ScenarioTrace model, scenario_runner instrumentation, and /traces API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScenarioTrace


# ── Model tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_trace_model_insert_and_retrieve(db_session: AsyncSession):
    trace = ScenarioTrace(
        scenario_name="email_triage",
        status="ok",
        trigger={"mailbox": "all"},
        steps_total=3,
        steps_done=3,
        step_traces=[
            {"step_id": "s1", "skill": "email.list", "status": "ok", "duration_ms": 10},
        ],
        duration_ms=120,
        started_at=datetime.now(timezone.utc),
        triggered_by="user:123",
    )
    db_session.add(trace)
    await db_session.flush()

    result = await db_session.execute(
        select(ScenarioTrace).where(ScenarioTrace.scenario_name == "email_triage")
    )
    found = result.scalar_one()
    assert found.status == "ok"
    assert found.steps_total == 3
    assert found.trigger == {"mailbox": "all"}
    assert found.triggered_by == "user:123"
    assert found.step_traces[0]["skill"] == "email.list"


@pytest.mark.asyncio
async def test_scenario_trace_model_error_state(db_session: AsyncSession):
    trace = ScenarioTrace(
        scenario_name="tp_from_drawing",
        status="error",
        steps_total=5,
        steps_done=2,
        step_traces=[],
        error="Step extract_surfaces: timeout",
        duration_ms=31000,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(trace)
    await db_session.flush()

    result = await db_session.execute(
        select(ScenarioTrace).where(ScenarioTrace.scenario_name == "tp_from_drawing")
    )
    found = result.scalar_one()
    assert found.status == "error"
    assert "timeout" in found.error
    assert found.steps_done == 2
    assert found.finished_at is None


@pytest.mark.asyncio
async def test_scenario_trace_model_timeout_state(db_session: AsyncSession):
    trace = ScenarioTrace(
        scenario_name="email_triage",
        status="timeout",
        steps_total=4,
        steps_done=1,
        step_traces=[],
        error="Scenario timed out after 300s",
        duration_ms=300_000,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(trace)
    await db_session.flush()

    result = await db_session.execute(
        select(ScenarioTrace).where(
            ScenarioTrace.scenario_name == "email_triage",
            ScenarioTrace.status == "timeout",
        )
    )
    found = result.scalar_one()
    assert found.status == "timeout"
    assert found.finished_at is not None


# ── Scenario runner instrumentation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_runner_collects_step_traces(monkeypatch):
    """runner.run() should create a _persist_trace task with step-level data."""
    from app.ai import scenario_runner as runner_module

    fake_scenario = {
        "timeout": 30,
        "steps": [
            {"id": "step_a", "skill": "email.list", "params": {}},
            {"id": "step_b", "skill": "invoice.list", "params": {}},
        ],
    }
    monkeypatch.setattr(runner_module.gateway_config, "load_scenario", lambda _: fake_scenario)

    persisted: list[dict] = []

    async def fake_persist(data: dict) -> None:
        persisted.append(data)

    async def fake_call_skill(name: str, params: dict) -> dict:
        return {"items": [], "skill": name}

    monkeypatch.setattr(runner_module, "_call_skill", fake_call_skill)
    monkeypatch.setattr(runner_module, "_persist_trace", fake_persist)

    tasks_created: list = []

    def mock_create_task(coro, **kwargs):
        tasks_created.append(coro)

        class FakeTask:
            pass

        return FakeTask()

    with patch("asyncio.create_task", side_effect=mock_create_task):
        result = await runner_module.scenario_runner.run(
            "email_triage", trigger={"test": True}, triggered_by="test_runner"
        )

    assert len(tasks_created) == 1
    await tasks_created[0]

    assert len(persisted) == 1
    trace = persisted[0]
    assert trace["scenario_name"] == "email_triage"
    assert trace["status"] == "ok"
    assert trace["steps_total"] == 2
    assert trace["steps_done"] == 2
    assert trace["triggered_by"] == "test_runner"
    assert len(trace["step_traces"]) == 2
    assert trace["step_traces"][0]["step_id"] == "step_a"
    assert trace["step_traces"][0]["skill"] == "email.list"
    assert trace["step_traces"][0]["status"] == "ok"
    assert trace["step_traces"][0]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_scenario_runner_records_step_error_without_aborting(monkeypatch):
    """on_error=continue: step error is recorded but scenario continues."""
    from app.ai import scenario_runner as runner_module

    fake_scenario = {
        "timeout": 30,
        "steps": [
            {"id": "fail_step", "skill": "broken.skill", "params": {}, "on_error": "continue"},
            {"id": "ok_step", "skill": "email.list", "params": {}},
        ],
    }
    monkeypatch.setattr(runner_module.gateway_config, "load_scenario", lambda _: fake_scenario)

    async def fake_call_skill(name: str, params: dict) -> dict:
        if name == "broken.skill":
            raise RuntimeError("broken")
        return {"items": []}

    monkeypatch.setattr(runner_module, "_call_skill", fake_call_skill)

    persisted: list[dict] = []

    async def fake_persist(data: dict) -> None:
        persisted.append(data)

    monkeypatch.setattr(runner_module, "_persist_trace", fake_persist)
    tasks_created: list = []

    def mock_create_task(coro, **kwargs):
        tasks_created.append(coro)

        class FakeTask:
            pass

        return FakeTask()

    with patch("asyncio.create_task", side_effect=mock_create_task):
        await runner_module.scenario_runner.run("email_triage")
    await tasks_created[0]

    trace = persisted[0]
    # on_error=continue: _run_step catches exception and returns {"error": ...}
    # so run() sees a normal return → step_status stays "ok"; error is in result_keys
    assert trace["status"] == "ok"
    assert trace["step_traces"][0]["status"] == "ok"
    assert trace["step_traces"][0]["result_keys"] == ["error"]
    assert trace["step_traces"][1]["status"] == "ok"


@pytest.mark.asyncio
async def test_scenario_runner_status_error_when_step_aborts(monkeypatch):
    """on_error=abort: scenario status set to 'error'."""
    from app.ai import scenario_runner as runner_module

    fake_scenario = {
        "timeout": 30,
        "steps": [
            {"id": "abort_step", "skill": "bad.skill", "params": {}, "on_error": "abort"},
        ],
    }
    monkeypatch.setattr(runner_module.gateway_config, "load_scenario", lambda _: fake_scenario)

    async def fake_call_skill(name: str, params: dict) -> dict:
        raise RuntimeError("critical failure")

    monkeypatch.setattr(runner_module, "_call_skill", fake_call_skill)

    persisted: list[dict] = []

    async def fake_persist(data: dict) -> None:
        persisted.append(data)

    monkeypatch.setattr(runner_module, "_persist_trace", fake_persist)
    tasks_created: list = []

    def mock_create_task(coro, **kwargs):
        tasks_created.append(coro)

        class FakeTask:
            pass

        return FakeTask()

    with patch("asyncio.create_task", side_effect=mock_create_task):
        await runner_module.scenario_runner.run("email_triage")
    await tasks_created[0]

    trace = persisted[0]
    assert trace["status"] == "error"
    assert "critical failure" in trace["error"]
    assert trace["step_traces"][0]["status"] == "error"


@pytest.mark.asyncio
async def test_scenario_runner_duration_ms_is_positive(monkeypatch):
    """duration_ms should be a non-negative integer."""
    from app.ai import scenario_runner as runner_module

    fake_scenario = {"timeout": 30, "steps": []}
    monkeypatch.setattr(runner_module.gateway_config, "load_scenario", lambda _: fake_scenario)

    persisted: list[dict] = []

    async def fake_persist(data: dict) -> None:
        persisted.append(data)

    monkeypatch.setattr(runner_module, "_persist_trace", fake_persist)
    tasks_created: list = []

    def mock_create_task(coro, **kwargs):
        tasks_created.append(coro)

        class FakeTask:
            pass

        return FakeTask()

    with patch("asyncio.create_task", side_effect=mock_create_task):
        await runner_module.scenario_runner.run("empty_scenario")
    await tasks_created[0]

    assert persisted[0]["duration_ms"] >= 0
    assert isinstance(persisted[0]["duration_ms"], int)


# ── /traces API tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_traces_endpoint_returns_list(client: AsyncClient, db_session: AsyncSession):
    trace = ScenarioTrace(
        scenario_name="email_triage",
        status="ok",
        steps_total=2,
        steps_done=2,
        step_traces=[],
        duration_ms=50,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(trace)
    await db_session.flush()

    resp = await client.get("/api/scenarios/traces")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any(t["scenario_name"] == "email_triage" for t in data)


@pytest.mark.asyncio
async def test_traces_endpoint_filter_by_name(client: AsyncClient, db_session: AsyncSession):
    for name in ("alpha_scenario", "beta_scenario", "alpha_scenario"):
        db_session.add(ScenarioTrace(
            scenario_name=name,
            status="ok",
            steps_total=1,
            steps_done=1,
            step_traces=[],
            started_at=datetime.now(timezone.utc),
        ))
    await db_session.flush()

    resp = await client.get("/api/scenarios/traces?scenario_name=alpha_scenario")
    assert resp.status_code == 200
    data = resp.json()
    assert all(t["scenario_name"] == "alpha_scenario" for t in data)
    assert len(data) >= 2


@pytest.mark.asyncio
async def test_traces_endpoint_limit_parameter(client: AsyncClient, db_session: AsyncSession):
    for i in range(5):
        db_session.add(ScenarioTrace(
            scenario_name=f"limit_test_{i}",
            status="ok",
            steps_total=1,
            steps_done=1,
            step_traces=[],
            started_at=datetime.now(timezone.utc),
        ))
    await db_session.flush()

    resp = await client.get("/api/scenarios/traces?limit=2")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) <= 2


@pytest.mark.asyncio
async def test_traces_endpoint_limit_max_200(client: AsyncClient):
    resp = await client.get("/api/scenarios/traces?limit=201")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_traces_endpoint_response_fields(client: AsyncClient, db_session: AsyncSession):
    trace = ScenarioTrace(
        scenario_name="field_check",
        status="error",
        trigger={"key": "val"},
        steps_total=3,
        steps_done=1,
        step_traces=[{"step_id": "s0", "status": "error"}],
        error="oops",
        duration_ms=999,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        triggered_by="user:99",
    )
    db_session.add(trace)
    await db_session.flush()

    resp = await client.get("/api/scenarios/traces?scenario_name=field_check")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    t = data[0]
    assert t["scenario_name"] == "field_check"
    assert t["status"] == "error"
    assert t["steps_total"] == 3
    assert t["steps_done"] == 1
    assert t["error"] == "oops"
    assert t["duration_ms"] == 999
    assert t["triggered_by"] == "user:99"
    assert t["finished_at"] is not None
    assert t["step_traces"][0]["step_id"] == "s0"
    assert "id" in t


@pytest.mark.asyncio
async def test_traces_endpoint_newest_first(client: AsyncClient, db_session: AsyncSession):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    for i, name in enumerate(["oldest", "middle", "newest"]):
        db_session.add(ScenarioTrace(
            scenario_name=name,
            status="ok",
            steps_total=0,
            steps_done=0,
            step_traces=[],
            started_at=now + timedelta(seconds=i),
        ))
    await db_session.flush()

    resp = await client.get(
        "/api/scenarios/traces?scenario_name=oldest&limit=3"
    )
    resp2 = await client.get("/api/scenarios/traces?limit=200")
    assert resp2.status_code == 200
    all_traces = resp2.json()
    names = [t["scenario_name"] for t in all_traces if t["scenario_name"] in ("oldest", "middle", "newest")]
    assert names.index("newest") < names.index("middle") < names.index("oldest")
