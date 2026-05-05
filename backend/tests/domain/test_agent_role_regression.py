from __future__ import annotations

from pathlib import Path

from app.ai.evals.agent_roles import (
    REQUIRED_ROLE_IDS,
    validate_agent_role_manifest,
)

ROOT = Path(__file__).resolve().parents[3]
ROLE_MANIFEST = ROOT / "backend/app/ai/evals/agent_role_cases.json"
SKILL_REGISTRY = ROOT / "aiagent/skills/_registry.yml"


def test_agent_role_regression_manifest_is_valid() -> None:
    errors = validate_agent_role_manifest(
        ROLE_MANIFEST,
        SKILL_REGISTRY,
    )

    assert errors == []


def test_agent_role_regression_covers_required_professions() -> None:
    import json

    manifest = json.loads(ROLE_MANIFEST.read_text(encoding="utf-8"))
    role_ids = {role["id"] for role in manifest["roles"]}

    assert role_ids == REQUIRED_ROLE_IDS
