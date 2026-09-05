"""AiAgent gateway configuration loader.

Single source of truth: aiagent/config/gateway.yml
Call gateway_config.reload() to hot-reload without server restart.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import structlog
import yaml

logger = structlog.get_logger()

_AIAGENT_ROOT = Path(
    os.environ.get(
        "AIAGENT_ROOT",
        # backend/app/ai/ → backend/app/ → backend/ → project_root/ → aiagent/
        str(Path(__file__).parent.parent.parent.parent / "aiagent"),
    )
)
_GATEWAY_PATH = _AIAGENT_ROOT / "config" / "gateway.yml"
_REGISTRY_PATH = _AIAGENT_ROOT / "skills" / "_registry.yml"
_CAPABILITIES_PATH = _AIAGENT_ROOT / "skills" / "capabilities.yml"
_PROMPTS_DIR = _AIAGENT_ROOT / "prompts"
_SCENARIOS_DIR = _AIAGENT_ROOT / "scenarios"


def _resolve_env(value: str) -> str:
    """Resolve ${VAR:-default} patterns from environment variables."""
    def replacer(m: re.Match) -> str:
        var, _, default = m.group(1).partition(":-")
        return os.environ.get(var, default)
    return re.sub(r"\$\{([^}]+)\}", replacer, str(value))


class GatewayConfig:
    """Reads aiagent/config/gateway.yml.

    All attributes are @property so changes to gateway.yml take effect on the
    next access without a server restart. Call .reload() explicitly if you need
    to flush the parsed cache (e.g. after a programmatic write).
    """

    def __init__(self) -> None:
        self._raw: dict = {}
        self.reload()

    def reload(self) -> None:
        """Re-read gateway.yml from disk."""
        if _GATEWAY_PATH.exists():
            self._raw = yaml.safe_load(_GATEWAY_PATH.read_text()) or {}

    # ── Skills ────────────────────────────────────────────────────────────────

    @property
    def exposed_skills(self) -> set[str]:
        """Whitelist of skills shown to the chat agent.

        A choice made in the interface (/settings/skills) wins over the file:
        gateway.yml is mounted read-only in the deployment, so the settings page
        could not persist anything and its Save button silently did nothing.
        """
        from app.api.ai_settings import get_ai_config

        try:
            override = get_ai_config().get("exposed_skills")
        except Exception:  # noqa: BLE001 — config store must never break routing
            override = None
        if isinstance(override, list) and override:
            return set(override)
        return set(self._raw.get("skills", {}).get("exposed", []))

    @property
    def approval_gates(self) -> set[str]:
        """Действия, требующие явного подтверждения человеком.

        В режиме capabilities источник — gate_actions манифеста, то есть ровно
        то, по чему принимает решение HTTP-граница. Список в gateway.yml
        остался от registry-режима и пользуется другой схемой имён
        (`invoice.approve` против `invoices.approve`): из 31 записи с
        манифестом совпадали 8. Оркестратор и agent_loop сверялись с этим
        списком и потому просто не находили большинство действий — заслон
        держала одна HTTP-граница вместо трёх.
        """
        if self.skills_mode == "capabilities":
            try:
                from app.ai.capability_manifest import load_capability_manifest

                return {
                    f"{cap.name}.{action}"
                    for cap in load_capability_manifest().capabilities
                    for action in (cap.gate_actions or ())
                }
            except Exception as exc:  # noqa: BLE001 — манифест может быть недоступен
                logger.warning("approval_gates_from_manifest_failed", error=str(exc))
        return set(self._raw.get("skills", {}).get("approval_gates", []))

    @property
    def skills_mode(self) -> str:
        """'capabilities' or 'registry'. Determines which skill file the agent uses."""
        return self._raw.get("skills", {}).get("mode", "registry")

    @property
    def capabilities_path(self) -> Path:
        rel = self._raw.get("skills", {}).get("capabilities")
        if rel:
            return _AIAGENT_ROOT / rel.lstrip("./")
        return _CAPABILITIES_PATH

    @property
    def registry_path(self) -> Path:
        return _REGISTRY_PATH

    @property
    def active_skills_path(self) -> Path:
        """Returns capabilities.yml in capabilities mode, _registry.yml otherwise."""
        if self.skills_mode == "capabilities" and self.capabilities_path.exists():
            return self.capabilities_path
        return _REGISTRY_PATH

    # ── Models ────────────────────────────────────────────────────────────────

    @property
    def reasoning_model(self) -> str:
        raw = self._raw.get("models", {}).get("reasoning", {}).get("model", "qwen3.5:9b")
        return _resolve_env(raw)

    @property
    def reasoning_base_url(self) -> str:
        raw = self._raw.get("models", {}).get("reasoning", {}).get(
            "base_url", "http://localhost:11434"
        )
        return _resolve_env(raw)

    @property
    def ocr_model(self) -> str:
        raw = self._raw.get("models", {}).get("ocr", {}).get("model", "qwen3.5:9b")
        return _resolve_env(raw)

    # ── Backend ───────────────────────────────────────────────────────────────

    @property
    def backend_url(self) -> str:
        raw = self._raw.get("backend", {}).get("base_url", "http://localhost:8000")
        return _resolve_env(raw).rstrip("/")

    @property
    def backend_timeout(self) -> int:
        return int(self._raw.get("backend", {}).get("timeout", 30))

    # ── Prompts ───────────────────────────────────────────────────────────────

    @property
    def base_prompt_path(self) -> Path:
        rel = self._raw.get("prompts", {}).get("base", "./prompts/base.md")
        return _AIAGENT_ROOT / rel.lstrip("./")

    def _role_entry(self, role: str) -> dict | None:
        """Role entry from prompts.roles — supports both legacy string form
        (``role: ./prompts/role-x.md``) and the object form
        (``role: {prompt: ..., capabilities: [...], default_canvases: [...]}``).
        """
        roles = self._raw.get("prompts", {}).get("roles", {})
        entry = roles.get(role)
        if entry is None:
            return None
        if isinstance(entry, str):
            return {"prompt": entry}
        if isinstance(entry, dict):
            return entry
        return None

    def role_prompt_path(self, role: str) -> Path | None:
        """Return the filesystem path of a role-specific prompt, or None."""
        entry = self._role_entry(role)
        rel = (entry or {}).get("prompt")
        if not rel:
            return None
        return _AIAGENT_ROOT / str(rel).lstrip("./")

    def role_prompt(self, role: str) -> str | None:
        """Return text of a role-specific prompt, or None if not found."""
        path = self.role_prompt_path(role)
        if not path:
            return None
        return path.read_text() if path.exists() else None

    def role_capabilities(self, role: str) -> list[str]:
        """Capability allowlist for the role ([] = no scoping declared)."""
        entry = self._role_entry(role)
        caps = (entry or {}).get("capabilities") or []
        return [str(cap) for cap in caps]

    def role_default_canvases(self, role: str) -> list[str]:
        entry = self._role_entry(role)
        canvases = (entry or {}).get("default_canvases") or []
        return [str(c) for c in canvases]

    # ── Scenarios ─────────────────────────────────────────────────────────────

    @property
    def scenario_definitions(self) -> list[dict]:
        """List of scenario descriptors from gateway.yml."""
        return self._raw.get("scenarios", [])

    def load_scenario(self, name: str) -> dict:
        """Load a scenario YAML by name or filename stem."""
        for sdef in self.scenario_definitions:
            if sdef.get("name") == name or Path(sdef.get("path", "")).stem == name:
                rel = sdef["path"]
                path = _AIAGENT_ROOT / rel.lstrip("./")
                if path.exists():
                    return yaml.safe_load(path.read_text()) or {}
        # Fallback: try direct file lookup
        path = _SCENARIOS_DIR / (name if name.endswith(".yml") else name + ".yml")
        if path.exists():
            return yaml.safe_load(path.read_text()) or {}
        return {}

    def list_scenario_names(self) -> list[str]:
        return [s["name"] for s in self.scenario_definitions if "name" in s]

    # ── Auth ──────────────────────────────────────────────────────────────────

    @property
    def agent_name(self) -> str:
        return self._raw.get("agent", {}).get("name", "Света")


# Module-level singleton — import and use directly.
# Changes to gateway.yml are reflected on next property access.
gateway_config = GatewayConfig()
