"""AiAgent gateway configuration loader.

Single source of truth: aiagent/config/gateway.yml
Call gateway_config.reload() to hot-reload without server restart.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

_AIAGENT_ROOT = Path(
    os.environ.get("AIAGENT_ROOT", str(Path(__file__).parent.parent.parent / "aiagent"))
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
        """Whitelist of skills shown to the chat agent."""
        return set(self._raw.get("skills", {}).get("exposed", []))

    @property
    def approval_gates(self) -> set[str]:
        """Skills that require explicit human confirmation before execution."""
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
        raw = self._raw.get("models", {}).get("ocr", {}).get("model", "gemma4:e4b")
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

    def role_prompt(self, role: str) -> str | None:
        """Return text of a role-specific prompt, or None if not found."""
        roles = self._raw.get("prompts", {}).get("roles", {})
        rel = roles.get(role)
        if not rel:
            return None
        path = _AIAGENT_ROOT / rel.lstrip("./")
        return path.read_text() if path.exists() else None

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
