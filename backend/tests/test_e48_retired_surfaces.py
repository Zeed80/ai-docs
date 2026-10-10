"""E48: retired generated-capability surfaces answer 410 and stay gone.

Generated skills, their evolution (shadow A/B promotion), the skill cache, the
capability sandbox and the ``skill-runner`` container were removed. These
tests pin that: old clients get an explicit 410, nothing in the runtime
imports the removed modules, no beat job or agent tool points at them, and a
capability proposal is recorded as a report without an approval card.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import uuid
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "backend" / "app"

REMOVED_MODULES = (
    "app.ai.capability_builder",
    "app.ai.capability_sandbox",
    "app.ai.skill_evolver",
    "app.ai.skill_cache",
    "app.ai.generated_skills",
    "app.tasks.skill_evolution",
)

RETIRED_ROUTES = (
    ("POST", "/api/agent/generated-skill/some_skill"),
    ("GET", "/api/agent/generated-skills"),
    ("GET", "/api/agent/skill-evolution/audit"),
    ("POST", "/api/agent/skill-evolution/evolve/some_skill"),
    ("GET", "/api/agent/skill-cache/stats"),
)

RETIRED_CAPABILITY_ACTIONS = ("sandbox-apply", "decide", "promote")


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), RETIRED_ROUTES)
async def test_retired_generated_skill_routes_answer_410(client, method, path):
    resp = await client.request(method, path, json={})
    assert resp.status_code == 410
    assert "retired" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", RETIRED_CAPABILITY_ACTIONS)
async def test_retired_capability_lifecycle_answers_410(client, action):
    body = {"approved": True, "decided_by": "user"} if action == "decide" else None
    resp = await client.post(f"/api/agent/capabilities/{uuid.uuid4()}/{action}", json=body)
    assert resp.status_code == 410


def test_removed_modules_are_gone():
    for name in REMOVED_MODULES:
        assert importlib.util.find_spec(name) is None, name


def test_runtime_does_not_import_removed_modules():
    offenders = []
    for path in APP_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n in REMOVED_MODULES for n in names):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_no_beat_job_or_task_for_skill_evolution():
    from app.tasks.celery_app import celery_app

    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert not {t for t in scheduled if t.startswith("skill.")}
    assert "skill.evolve_failing_skills" not in celery_app.tasks
    assert "skill.evaluate_shadow_tests" not in celery_app.tasks


def test_no_agent_tool_points_at_a_retired_route():
    gateway = yaml.safe_load((REPO_ROOT / "aiagent/config/gateway.yml").read_text("utf-8"))
    granted = set()
    for value in gateway.values():
        if isinstance(value, list):
            granted |= {v for v in value if isinstance(v, str)}
    assert "capability.sandbox_apply" not in granted

    for name in ("_registry.json", "registry.json"):
        tools = json.loads((REPO_ROOT / "aiagent/skills" / name).read_text("utf-8"))["tools"]
        paths = {tool.get("path", "") for tool in tools}
        assert not {
            p
            for p in paths
            if (
                p.startswith("/api/agent/capabilities/")
                and p.rsplit("/", 1)[-1] in RETIRED_CAPABILITY_ACTIONS
            )
            or "generated-skill" in p
            or "skill-evolution" in p
            or "skill-cache" in p
        }, name


def test_retired_routes_are_hidden_from_the_schema():
    from app.main import app

    schema_paths = set(app.openapi()["paths"])
    assert not {p for p in schema_paths if "generated-skill" in p or "skill-evolution" in p}
    assert not {
        p
        for p in schema_paths
        if p.startswith("/api/agent/capabilities/")
        and p.rsplit("/", 1)[-1] in RETIRED_CAPABILITY_ACTIONS
    }


def test_skill_runner_container_is_gone():
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        services = yaml.safe_load((REPO_ROOT / "infra" / name).read_text("utf-8"))["services"]
        assert "skill-runner" not in services, name


def test_persisted_config_cannot_reactivate_the_builder():
    from app.ai.agent_config import BuiltinAgentConfig

    assert BuiltinAgentConfig().allow_capability_builder is False
    assert BuiltinAgentConfig(allow_capability_builder=True).allow_capability_builder is False


@pytest.mark.asyncio
async def test_proposal_is_a_report_without_an_approval_card(client, monkeypatch):
    from app.core.chat_bus import chat_bus

    published: list[dict] = []

    async def capture(event, *args, **kwargs):
        published.append(event)

    monkeypatch.setattr(chat_bus, "publish", capture)
    resp = await client.post(
        "/api/agent/capabilities/propose",
        json={
            "title": "E48 report",
            "missing_capability": "export to a format nobody supports",
            "reason": "no tool",
            "draft": {},
            "risk_level": "critical",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["status"] == "draft"
    assert not [e for e in published if e.get("tool") == "capability.proposal"]
