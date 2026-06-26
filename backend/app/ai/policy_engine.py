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
    # High-impact operational settings — changes affect agent behaviour significantly
    "disable_thinking",
    "max_steps",
    "llm_timeout_seconds",
    "backend_timeout_seconds",
    "autonomy_mode",
    # RCE vector: StdioMCPClient executes `command` from this config via subprocess
    "mcp_servers",
    # Prevent silent model-provider switching or skill surface expansion
    "provider",
    "exposed_skills",
    # Data-flow boundary: quality checks may only go to cloud after an
    # explicit human decision (Dual AI principle).
    "auditor_allow_cloud",
}

EXTERNAL_SKILL_PREFIXES = ("email.", "telegram.", "export.", "procurement.")

# Markers that indicate a high-risk legacy registry skill (dot-separated names).
# Capability-mode tools (no dots) have their gate_actions handled by agent_loop
# directly from capabilities.yml, so these markers only apply to registry tools.
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
    ".resolve",
    ".apply_diff",
)

RISKY_CAPABILITY_ACTIONS = {
    "approve",
    "reject",
    "delete",
    "bulk_delete",
    "bulk_approve",
    "bulk_reject",
    "send",
    "send_rfq",
    "mark_paid",
    "activate_rule",
    "apply_rules",
    "confirm_receipt",
    "bulk_confirm",
    "issue_stock",
    "resolve",
    "apply_diff",
    "decide",
    "export_1c",
    "create_contract",
    "process_plan_approve",
    "norm_estimate_approve",
    "learning_rule_activate",
    "table_apply_diff",
    "table_import_excel",
    "spec_table_cell_edit",
    "ai_config_set",
}


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
    # Capability-mode tools have no dot — risk is handled via gate_actions in capabilities.yml.
    if "." not in skill_name and not skill_name.startswith(EXTERNAL_SKILL_PREFIXES):
        return "low"
    if any(marker in skill_name for marker in DESTRUCTIVE_SKILL_MARKERS):
        return "high"
    if skill_name.startswith(EXTERNAL_SKILL_PREFIXES):
        return "medium"
    return "low"


def classify_capability_action_risk(action: str | None) -> str:
    """Risk class for broad capability-mode actions.

    Broad tools such as ``invoices`` or ``documents`` hide the specific action
    from the tool name, so dotted-name marker checks cannot protect them. This
    conservative marker list makes missing ``gate_actions`` fail closed.
    """
    normalized = str(action or "").strip()
    if not normalized:
        return "low"
    if normalized == "learning_rule_reject":
        return "low"
    if normalized in RISKY_CAPABILITY_ACTIONS:
        return "high"
    if normalized.startswith(("delete_", "bulk_", "approve_", "reject_", "send_")):
        return "high"
    if normalized.endswith(("_approve", "_reject", "_delete", "_send", "_export_1c")):
        return "high"
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
    action = args.get("action")
    action_risk = (
        classify_capability_action_risk(str(action))
        if "." not in skill_name
        else "low"
    )
    if action_risk == "high":
        risk = "high"
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
            reason=(
                f"{skill_name}"
                + (f".{action}" if action and "." not in skill_name else "")
                + " is high-risk and must be listed in approval_gates"
            ),
            required_approval=True,
            risk_level=risk,
        )

    return PolicyDecision(allowed=True, risk_level=risk)
