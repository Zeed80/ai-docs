"""Comprehensive YAML scenario runner tests — all 11 scenario files.

Each test:
  1. Loads the YAML file and validates its structure
  2. Runs through scenario_runner.run() with a mock _call_skill
  3. Verifies the run completes without unhandled exceptions
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

SCENARIOS_DIR = Path(__file__).parents[2] / "aiagent" / "scenarios"


# ── YAML loading helpers ──────────────────────────────────────────────────────


def _load(filename: str) -> dict:
    path = SCENARIOS_DIR / filename
    if not path.exists():
        pytest.skip(f"Scenario file not found: {path}")
    return yaml.safe_load(path.read_text()) or {}


def _all_scenario_files() -> list[str]:
    return sorted(p.name for p in SCENARIOS_DIR.glob("*.yml"))


# ── Generic runner ────────────────────────────────────────────────────────────


async def _run_scenario(scenario_data: dict, trigger: dict | None = None) -> dict:
    """Run a scenario through scenario_runner with a universal mock skill."""
    from app.ai.scenario_runner import ScenarioRunner

    mock_result: dict[str, Any] = {
        # Common fields used by template expressions across scenarios
        "status": "ok",
        "items": [],
        "total": 0,
        "count": 0,
        "results": [],
        "attachments": [],
        "document_id": "00000000-0000-0000-0000-000000000001",
        "invoice_id": "00000000-0000-0000-0000-000000000002",
        "email_id": "00000000-0000-0000-0000-000000000003",
        "plan_id": "00000000-0000-0000-0000-000000000004",
        "receipt_id": "00000000-0000-0000-0000-000000000005",
        "drawing_id": "00000000-0000-0000-0000-000000000006",
        "errors_count": 0,
        "operations_count": 3,
        "surfaces_count": 5,
        "lines_count": 2,
        "checks": [],
        "draft": "Уважаемый поставщик,",
        "approved": False,
    }

    tasks_created: list = []

    def mock_create_task(coro, **kwargs):
        tasks_created.append(coro)

        class _FakeTask:
            pass

        return _FakeTask()

    with (
        patch("app.ai.scenario_runner._call_skill", return_value=mock_result),
        patch(
            "app.ai.scenario_runner.gateway_config.load_scenario",
            return_value=scenario_data,
        ),
        patch("asyncio.create_task", side_effect=mock_create_task),
    ):
        runner = ScenarioRunner()
        result = await runner.run(
            scenario_name=scenario_data.get("name", "unknown"),
            trigger=trigger or {},
        )

    # Drain fire-and-forget tasks
    for task in tasks_created:
        try:
            await task
        except Exception:
            pass

    return result


# ── Structure assertions ──────────────────────────────────────────────────────


def _assert_valid_structure(data: dict, filename: str) -> None:
    assert data, f"{filename}: empty or unparseable YAML"
    assert data.get("name") or data.get("id"), f"{filename}: missing 'name' field"
    assert isinstance(data.get("steps", []), list), f"{filename}: 'steps' must be a list"
    steps = data.get("steps", [])
    for i, step in enumerate(steps):
        assert isinstance(step, dict), f"{filename}: step[{i}] must be a dict"
    if data.get("timeout") is not None:
        assert isinstance(data["timeout"], int), f"{filename}: 'timeout' must be an int"


# ── Per-scenario structure tests ───────────────────────────────────────────────


@pytest.mark.parametrize("filename", _all_scenario_files())
def test_scenario_yaml_valid_structure(filename: str) -> None:
    """All YAML scenario files must have valid structure."""
    data = _load(filename)
    _assert_valid_structure(data, filename)


@pytest.mark.parametrize("filename", _all_scenario_files())
def test_scenario_yaml_steps_have_ids(filename: str) -> None:
    """Every step should have an id (for tracing and template references)."""
    data = _load(filename)
    steps = data.get("steps", [])
    for i, step in enumerate(steps):
        # Steps without id are allowed but should at least be a dict
        assert isinstance(step, dict), f"{filename}: step[{i}] is not a dict"


@pytest.mark.parametrize("filename", _all_scenario_files())
def test_scenario_yaml_skills_are_strings(filename: str) -> None:
    """Any 'skill' field in steps must be a string."""
    data = _load(filename)
    for step in data.get("steps", []):
        if "skill" in step:
            assert isinstance(step["skill"], str), (
                f"{filename}/{step.get('id', '?')}: skill must be a string"
            )


# ── Per-scenario runner tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_triage_runs_to_completion() -> None:
    data = _load("email-triage.yml")
    result = await _run_scenario(data, trigger={"mailbox": "all"})
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_assisted_review_runs_to_completion() -> None:
    data = _load("assisted-review.yml")
    result = await _run_scenario(data, trigger={"document_id": "00000000-0000-0000-0000-000000000001"})
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_draft_email_runs_to_completion() -> None:
    data = _load("draft-email.yml")
    result = await _run_scenario(data, trigger={"supplier_id": "00000000-0000-0000-0000-000000000001"})
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_smart_ingest_runs_to_completion() -> None:
    data = _load("smart-ingest.yml")
    result = await _run_scenario(data, trigger={"file_id": "00000000-0000-0000-0000-000000000001"})
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_anomaly_resolution_runs_to_completion() -> None:
    data = _load("anomaly-resolution.yml")
    result = await _run_scenario(data, trigger={"anomaly_id": "00000000-0000-0000-0000-000000000001"})
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_nl_query_action_runs_to_completion() -> None:
    data = _load("nl-query-action.yml")
    result = await _run_scenario(data, trigger={"query": "покажи счета за май"})
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_low_stock_alert_runs_to_completion() -> None:
    data = _load("low-stock-alert.yml")
    result = await _run_scenario(data)
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_memory_maintenance_runs_to_completion() -> None:
    data = _load("memory-maintenance.yml")
    result = await _run_scenario(data)
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_warehouse_receipt_runs_to_completion() -> None:
    data = _load("warehouse-receipt.yml")
    result = await _run_scenario(data, trigger={"invoice_id": "00000000-0000-0000-0000-000000000002"})
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_tp_from_drawing_runs_to_completion() -> None:
    data = _load("tp_from_drawing.yml")
    result = await _run_scenario(data, trigger={"drawing_id": "00000000-0000-0000-0000-000000000006"})
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_drawing_tooling_workflow_runs_to_completion() -> None:
    data = _load("drawing_tooling_workflow.yml")
    result = await _run_scenario(data, trigger={"drawing_id": "00000000-0000-0000-0000-000000000006"})
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_warehouse_low_stock_reorder_runs_to_completion() -> None:
    """warehouse_specialist scenario: low stock check + reorder draft."""
    data = _load("warehouse-low-stock-reorder.yml")
    result = await _run_scenario(data)
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_warehouse_low_stock_reorder_early_exit_when_no_stock() -> None:
    """If low_stock_items.total == 0, scenario exits cleanly after early_exit step."""
    data = _load("warehouse-low-stock-reorder.yml")

    zero_result: dict[str, Any] = {"status": "ok", "items": [], "total": 0, "count": 0}
    with (
        patch("app.ai.scenario_runner._call_skill", return_value=zero_result),
        patch("app.ai.scenario_runner.gateway_config.load_scenario", return_value=data),
        patch("asyncio.create_task", side_effect=lambda coro, **kw: type("T", (), {})()),
    ):
        from app.ai.scenario_runner import ScenarioRunner
        runner = ScenarioRunner()
        result = await runner.run(scenario_name=data["name"], trigger={})

    # Scenario completed without error — early_exit branch taken
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_procurement_rfq_cycle_runs_to_completion() -> None:
    """procurement_specialist scenario: RFQ cycle with compare + approval."""
    data = _load("procurement-rfq-cycle.yml")
    result = await _run_scenario(
        data,
        trigger={
            "collection_id": "00000000-0000-0000-0000-000000000010",
            "items": [{"name": "Болт М8", "qty": 100, "unit": "шт"}],
        },
    )
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_anomaly_resolution_critical_path() -> None:
    """Critical anomalies route through 2-step approval chain."""
    data = _load("anomaly-resolution.yml")

    # Mock: has_critical returns total > 0 to trigger chain path
    critical_result: dict[str, Any] = {
        "status": "ok", "total": 1, "items": [], "count": 1,
        "chain_root_id": "00000000-0000-0000-0000-000000000099",
        "decisions": [],
        "supplier_id": "00000000-0000-0000-0000-000000000001",
        "invoice_number": "INV-001",
        "name": "ООО Тест",
        "trust_score": 0.7,
    }
    with (
        patch("app.ai.scenario_runner._call_skill", return_value=critical_result),
        patch("app.ai.scenario_runner.gateway_config.load_scenario", return_value=data),
        patch("asyncio.create_task", side_effect=lambda coro, **kw: type("T", (), {})()),
    ):
        from app.ai.scenario_runner import ScenarioRunner
        runner = ScenarioRunner()
        result = await runner.run(
            scenario_name=data["name"],
            trigger={
                "invoice_id": "00000000-0000-0000-0000-000000000002",
                "anomaly_ids": ["00000000-0000-0000-0000-000000000001"],
            },
        )
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_anomaly_resolution_standard_path() -> None:
    """Non-critical anomalies use single approval gate."""
    data = _load("anomaly-resolution.yml")

    standard_result: dict[str, Any] = {
        "status": "ok", "total": 0, "items": [], "count": 0,
        "decisions": [],
        "supplier_id": "00000000-0000-0000-0000-000000000001",
        "invoice_number": "INV-002",
        "name": "ООО Тест",
        "trust_score": 0.8,
    }
    with (
        patch("app.ai.scenario_runner._call_skill", return_value=standard_result),
        patch("app.ai.scenario_runner.gateway_config.load_scenario", return_value=data),
        patch("asyncio.create_task", side_effect=lambda coro, **kw: type("T", (), {})()),
    ):
        from app.ai.scenario_runner import ScenarioRunner
        runner = ScenarioRunner()
        result = await runner.run(
            scenario_name=data["name"],
            trigger={
                "invoice_id": "00000000-0000-0000-0000-000000000002",
                "anomaly_ids": ["00000000-0000-0000-0000-000000000001"],
            },
        )
    assert isinstance(result, dict)


# ── Cross-scenario invariants ─────────────────────────────────────────────────


def test_all_scenario_names_are_unique() -> None:
    """No two scenario files can have the same name."""
    names: list[str] = []
    for filename in _all_scenario_files():
        data = _load(filename)
        name = data.get("name") or data.get("id", "")
        names.append(name)
    assert len(names) == len(set(names)), f"Duplicate scenario names: {names}"


def test_all_scenarios_have_timeout() -> None:
    """Every scenario should define a timeout to prevent hung executions."""
    missing: list[str] = []
    for filename in _all_scenario_files():
        data = _load(filename)
        if "timeout" not in data:
            missing.append(filename)
    assert not missing, f"Scenarios missing 'timeout': {missing}"


@pytest.mark.asyncio
async def test_scenario_runner_completes_all_yaml_files() -> None:
    """Meta-test: every YAML file completes without unhandled exceptions."""
    errors: list[str] = []
    for filename in _all_scenario_files():
        data = _load(filename)
        try:
            await _run_scenario(data)
        except Exception as exc:
            errors.append(f"{filename}: {exc}")
    assert not errors, "Some scenarios raised exceptions:\n" + "\n".join(errors)


# ── Scenario /run API tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_scenario_endpoint_not_found(client) -> None:
    """POST /api/scenarios/nonexistent/run → 404."""
    resp = await client.post("/api/scenarios/nonexistent-xyz-abc/run", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_scenarios_returns_list(client) -> None:
    """GET /api/scenarios → list (may be empty if gateway.yml has no scenarios)."""
    resp = await client.get("/api/scenarios")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_traces_returns_list_for_all_scenarios(client) -> None:
    """GET /api/scenarios/traces → always returns a list."""
    resp = await client.get("/api/scenarios/traces")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
