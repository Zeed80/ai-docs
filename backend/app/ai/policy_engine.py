"""Policy checks for agent tool execution and self-configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai.agent_config import BuiltinAgentConfig

PROTECTED_SETTINGS = {
    "agent_name",
    "system_prompt",
    "memory_enabled",
    "audit_enabled",
    "approval_gates",
    "allow_capability_builder",
    "capability_builder_requires_approval",
    "permission_mode",
    "safe_auto_apply_enabled",
}

EXTERNAL_SKILL_PREFIXES = ("email.", "telegram.", "export.", "procurement.")
DESTRUCTIVE_SKILL_MARKERS = (
    ".delete",
    ".bulk_delete",
    ".send",
    ".approve",
    ".reject",
    ".decide",
    ".mark_paid",
    ".activate_rule",
    ".confirm_receipt",
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""
    required_approval: bool = False
    risk_level: str = "low"


def is_protected_setting(setting_path: str) -> bool:
    root = setting_path.split(".", 1)[0]
    return root in PROTECTED_SETTINGS


def classify_skill_risk(skill_name: str) -> str:
    if any(marker in skill_name for marker in DESTRUCTIVE_SKILL_MARKERS):
        return "high"
    if skill_name.startswith(EXTERNAL_SKILL_PREFIXES):
        return "medium"
    return "low"


def check_tool_execution(
    *,
    skill_name: str,
    args: dict[str, Any],
    config: BuiltinAgentConfig,
    approval_gates: set[str],
) -> PolicyDecision:
    """Return a policy decision before a skill/tool is executed."""
    risk = classify_skill_risk(skill_name)
    mode = (config.permission_mode or "workspace_write").lower()
    local_only = bool(args.get("local_only") is True or args.get("confidential") is True)

    if mode in {"read_only", "read-only"} and risk != "low":
        return PolicyDecision(
            allowed=False,
            reason=f"{skill_name} requires write/external permission in read-only mode",
            required_approval=True,
            risk_level=risk,
        )

    if skill_name.startswith(EXTERNAL_SKILL_PREFIXES) and local_only:
        return PolicyDecision(
            allowed=False,
            reason=f"{skill_name} is external but request is local_only/confidential",
            required_approval=True,
            risk_level="high",
        )

    if risk == "high" and skill_name not in approval_gates:
        return PolicyDecision(
            allowed=False,
            reason=f"{skill_name} is high-risk and must be listed in approval_gates",
            required_approval=True,
            risk_level=risk,
        )

    return PolicyDecision(allowed=True, risk_level=risk)

