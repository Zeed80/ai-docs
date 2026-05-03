from __future__ import annotations

from pathlib import Path

from app.ai.evals.agent_roles import (
    REQUIRED_ROLE_IDS,
    validate_agent_role_manifest,
)


def test_agent_role_regression_manifest_is_valid() -> None:
    errors = validate_agent_role_manifest(
        Path("app/ai/evals/agent_role_cases.json"),
        Path("/aiagent/skills/_registry.yml"),
    )

    assert errors == []


def test_agent_role_regression_covers_required_professions() -> None:
    import json

    manifest = json.loads(
        Path("app/ai/evals/agent_role_cases.json").read_text(encoding="utf-8")
    )
    role_ids = {role["id"] for role in manifest["roles"]}

    assert role_ids == REQUIRED_ROLE_IDS
