"""Tests for Scenario 9 — tp_from_drawing.yml via scenario_runner.py mock."""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

_SCENARIO_PATH = (
    Path(__file__).parents[2] / "aiagent" / "scenarios" / "tp_from_drawing.yml"
)


def _load_scenario_yaml() -> dict:
    if _SCENARIO_PATH.exists():
        return yaml.safe_load(_SCENARIO_PATH.read_text()) or {}
    return {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _drawing_result(drawing_id: str, status: str = "analyzed") -> dict:
    return {"drawing_id": drawing_id, "status": status, "features_count": 5}


def _plan_result(plan_id: str) -> dict:
    return {"plan_id": plan_id, "status": "draft"}


def _tp_gen_result(plan_id: str) -> dict:
    return {
        "plan_id": plan_id,
        "task_id": str(uuid.uuid4()),
        "operations_count": 5,
        "surfaces_count": 8,
    }


def _plan_get_result(plan_id: str) -> dict:
    return {
        "plan_id": plan_id,
        "product_name": "Вал",
        "material": "Ст.45",
        "blank_type": "прокат",
        "operations_count": 5,
        "route_summary": "005 Токарная → 010 Фрезерная → 015 Контроль",
    }


def _nc_passed() -> dict:
    return {
        "status": "passed",
        "checks": [],
        "errors_count": 0,
        "warnings_count": 0,
    }


def _nc_failed() -> dict:
    return {
        "status": "failed",
        "checks": [
            {"check_code": "ESTD_MK_001", "severity": "error", "message": "Материал не указан"}
        ],
        "errors_count": 1,
        "warnings_count": 0,
    }


# ── ScenarioRunner import guard ────────────────────────────────────────────────

@pytest.fixture
def runner_cls():
    try:
        from app.ai.scenario_runner import ScenarioRunner
        return ScenarioRunner
    except ImportError:
        pytest.skip("ScenarioRunner not available")


# ── Happy path ────────────────────────────────────────────────────────────────

def _scenario_ctx(runner_cls, mock_skill, drawing_id):
    """Context manager: patches both _call_skill and gateway_config.load_scenario."""
    from unittest.mock import patch as _patch
    scenario_data = _load_scenario_yaml()
    return (
        _patch("app.ai.scenario_runner._call_skill", mock_skill),
        _patch(
            "app.ai.scenario_runner.gateway_config.load_scenario",
            return_value=scenario_data,
        ),
    )


@pytest.mark.asyncio
async def test_happy_path_normcontrol_passed(runner_cls):
    """All steps succeed, normcontrol passes → context returned."""
    drawing_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())

    mock_skill = AsyncMock(return_value={
        "plan_id": plan_id,
        "drawing_id": drawing_id,
        "status": "passed",
        "errors_count": 0,
        "operations_count": 5,
    })

    scenario_data = _load_scenario_yaml()

    with (
        patch("app.ai.scenario_runner._call_skill", mock_skill),
        patch("app.ai.scenario_runner.gateway_config.load_scenario", return_value=scenario_data),
    ):
        runner = runner_cls()
        result = await runner.run(
            scenario_name="tp_from_drawing",
            trigger={"drawing_id": drawing_id},
        )

    assert isinstance(result, dict)
    assert mock_skill.call_count >= 1


@pytest.mark.asyncio
async def test_normcontrol_failure_does_not_approve(runner_cls):
    """Normcontrol fails → run completes without calling approve skill."""
    drawing_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())

    mock_skill = AsyncMock(return_value={
        "plan_id": plan_id,
        "drawing_id": drawing_id,
        "status": "failed",
        "errors_count": 1,
    })

    scenario_data = _load_scenario_yaml()

    with (
        patch("app.ai.scenario_runner._call_skill", mock_skill),
        patch("app.ai.scenario_runner.gateway_config.load_scenario", return_value=scenario_data),
    ):
        runner = runner_cls()
        await runner.run(
            scenario_name="tp_from_drawing",
            trigger={"drawing_id": drawing_id},
        )

    # Verify approve was NOT called
    called_names = [str(c) for c in mock_skill.call_args_list]
    assert not any("process_plan_approve" in s for s in called_names)


# ── Scenario YAML existence and structure ──────────────────────────────────────

def test_scenario_yaml_exists():
    """Scenario 9 YAML file must exist and be parseable."""
    import yaml
    from pathlib import Path

    path = Path(__file__).parents[2] / "aiagent" / "scenarios" / "tp_from_drawing.yml"
    assert path.exists(), f"Scenario file not found: {path}"

    with open(path) as f:
        data = yaml.safe_load(f)

    assert data is not None
    # Must have id/name and steps or trigger
    assert data.get("id") or data.get("name")
    assert "steps" in data or "trigger" in data


def test_scenario_has_normcontrol_step():
    """Scenario must include a normcontrol_check step."""
    from pathlib import Path

    path = Path(__file__).parents[2] / "aiagent" / "scenarios" / "tp_from_drawing.yml"
    with open(path) as f:
        content = f.read()

    assert "normcontrol" in content.lower()


def test_scenario_has_approval_gate():
    """Scenario must define an approval gate."""
    from pathlib import Path

    path = Path(__file__).parents[2] / "aiagent" / "scenarios" / "tp_from_drawing.yml"
    with open(path) as f:
        content = f.read()

    assert "approval" in content.lower() or "approval_required" in content.lower()


def test_scenario_has_generate_tp_skill():
    """Scenario must call tech.generate_tp_from_drawing."""
    from pathlib import Path

    path = Path(__file__).parents[2] / "aiagent" / "scenarios" / "tp_from_drawing.yml"
    with open(path) as f:
        content = f.read()

    assert "generate_tp" in content or "generate-tp" in content or "tech.generate" in content


def test_scenario_worker_role_is_technologist():
    """Scenario must assign worker_role: technologist."""
    from pathlib import Path

    path = Path(__file__).parents[2] / "aiagent" / "scenarios" / "tp_from_drawing.yml"
    with open(path) as f:
        content = f.read()

    assert "technologist" in content


# ── Drawing not analyzed triggers re-analysis ──────────────────────────────────

@pytest.mark.asyncio
async def test_drawing_not_analyzed_scenario_still_runs(runner_cls):
    """Scenario runs even if drawing starts in 'uploaded' state."""
    drawing_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())

    mock_skill = AsyncMock(return_value={"status": "ok", "plan_id": plan_id, "drawing_id": drawing_id})
    scenario_data = _load_scenario_yaml()

    with (
        patch("app.ai.scenario_runner._call_skill", mock_skill),
        patch("app.ai.scenario_runner.gateway_config.load_scenario", return_value=scenario_data),
    ):
        runner = runner_cls()
        result = await runner.run(
            scenario_name="tp_from_drawing",
            trigger={"drawing_id": drawing_id},
        )

    assert result is not None
    assert mock_skill.call_count >= 1
