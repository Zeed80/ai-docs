"""Isolation contract for agent-generated code.

Generated code must never execute inside the backend process; the dedicated
skill-runner container must run locked down and credential-free.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "infra" / "docker-compose.yml"

# Env vars that count as credentials/secrets — none may reach the runner.
_SECRET_MARKERS = ("PASSWORD", "SECRET", "KEY", "TOKEN")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_compose_runner_is_locked_down():
    services = _compose()["services"]
    assert "skill-runner" in services, "skill-runner service must exist"
    runner = services["skill-runner"]

    assert runner.get("read_only") is True
    assert any("no-new-privileges" in opt for opt in runner.get("security_opt", []))
    assert runner.get("mem_limit"), "memory limit required"
    assert runner.get("pids_limit"), "pids limit required"

    env = runner.get("environment") or {}
    env_items = env.items() if isinstance(env, dict) else [(e.split("=")[0], "") for e in env]
    for key, _ in env_items:
        assert not any(marker in key.upper() for marker in _SECRET_MARKERS), (
            f"skill-runner environment must not contain credentials: {key}"
        )

    mounts = runner.get("volumes") or []
    skills_mounts = [m for m in mounts if "generated_skills" in str(m)]
    assert skills_mounts, "generated_skills must be mounted"
    assert all(str(m).endswith(":ro") for m in skills_mounts), (
        "generated_skills mount must be read-only"
    )


def test_backend_module_never_imports_generated_code():
    """The proxy module must contain no importlib/module-exec machinery."""
    source = (REPO_ROOT / "backend/app/api/dynamic_skill_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name.split(".")[0] for alias in node.names}
            assert "importlib" not in names, "in-process import of generated code is forbidden"
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "importlib"
    assert "exec_module" not in source
    assert "spec_from_file_location" not in source


@pytest.mark.asyncio
async def test_run_generated_skill_proxies_to_runner(client, monkeypatch):
    """The endpoint forwards execution to SKILL_RUNNER_URL over HTTP."""
    import httpx

    from app.api import dynamic_skill_runner as dsr

    posted: list[tuple[str, dict]] = []

    class FakeResponse:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"status": "ok", "echo": True}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):  # noqa: A002
            posted.append((url, json or {}))
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    # Bypass cache/shadow side machinery.
    monkeypatch.setattr(dsr, "_GENERATED_ROOT", Path("/nonexistent"))

    resp = await client.post(
        "/api/agent/generated-skill/some_skill", json={"x": 1}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert posted, "execution must be proxied to the skill-runner"
    url, payload = posted[0]
    from app.config import settings
    assert url.startswith(settings.skill_runner_url)
    assert url.endswith("/run/some_skill")
    assert payload == {"args": {"x": 1}}


def test_generated_capability_registration(tmp_path, monkeypatch):
    """Promotion registers the skill in capabilities.generated.yml (separate file)."""
    from app.ai import capability_sandbox, gateway_config

    cap_path = tmp_path / "capabilities.yml"
    cap_path.write_text("version: 2\ncapabilities: []\n", encoding="utf-8")
    monkeypatch.setattr(
        type(gateway_config.gateway_config), "capabilities_path",
        property(lambda self: cap_path),
    )

    assert capability_sandbox._add_generated_capability("my.skill", "Описание") is True
    gen_path = tmp_path / "capabilities.generated.yml"
    assert gen_path.exists()
    data = yaml.safe_load(gen_path.read_text(encoding="utf-8"))
    entry = data["generated"][0]
    assert entry["name"] == "my_skill"
    assert entry["path"] == "/api/agent/generated-skill/my_skill"
    # Idempotent.
    assert capability_sandbox._add_generated_capability("my.skill", "Описание") is True
    data = yaml.safe_load(gen_path.read_text(encoding="utf-8"))
    assert len(data["generated"]) == 1
    # The hand-written capabilities.yml is untouched.
    assert cap_path.read_text(encoding="utf-8") == "version: 2\ncapabilities: []\n"
