from app.ai.agent_config import BuiltinAgentConfig
from app.ai.policy_engine import (
    check_tool_execution,
    classify_capability_action_risk,
    is_protected_setting,
)


def test_protected_settings_include_agent_identity_and_safety_flags():
    assert is_protected_setting("agent_name")
    assert is_protected_setting("system_prompt")
    assert is_protected_setting("approval_gates")
    assert not is_protected_setting("temperature")


def test_high_risk_skill_requires_approval_gate():
    config = BuiltinAgentConfig(permission_mode="workspace_write")
    decision = check_tool_execution(
        skill_name="email.send",
        args={},
        config=config,
        approval_gates=set(),
    )

    assert decision.allowed is False
    assert decision.required_approval is True
    assert decision.risk_level == "high"


def test_local_only_blocks_external_skill():
    config = BuiltinAgentConfig(permission_mode="workspace_write")
    decision = check_tool_execution(
        skill_name="email.search",
        args={"local_only": True},
        config=config,
        approval_gates={"email.search"},
    )

    assert decision.allowed is False
    assert "local_only" in decision.reason


def test_capability_action_risk_is_detected_for_broad_tools():
    assert classify_capability_action_risk("approve") == "high"
    assert classify_capability_action_risk("bulk_delete") == "high"
    assert classify_capability_action_risk("bom_approve") == "high"
    assert classify_capability_action_risk("table_export_1c") == "high"
    assert classify_capability_action_risk("compare_decide") == "high"
    assert classify_capability_action_risk("delete_template") == "high"
    assert classify_capability_action_risk("templates.delete") == "high"
    assert classify_capability_action_risk("list") == "low"


def test_template_delete_alias_requires_its_own_broad_capability_gate():
    config = BuiltinAgentConfig(permission_mode="workspace_write")
    denied = check_tool_execution(
        skill_name="email",
        args={"action": "templates.delete"},
        config=config,
        approval_gates=set(),
    )
    canonical_gate_does_not_authorize_alias = check_tool_execution(
        skill_name="email",
        args={"action": "templates.delete"},
        config=config,
        approval_gates={"email.delete_template"},
    )
    alias_gate = check_tool_execution(
        skill_name="email",
        args={"action": "templates.delete"},
        config=config,
        approval_gates={"email.templates.delete"},
    )
    canonical_action = check_tool_execution(
        skill_name="email",
        args={"action": "delete_template"},
        config=config,
        approval_gates={"email.delete_template"},
    )
    direct_alias = check_tool_execution(
        skill_name="email.templates.delete",
        args={},
        config=config,
        approval_gates=set(),
    )

    assert denied.allowed is False
    assert denied.required_approval is True
    assert canonical_gate_does_not_authorize_alias.allowed is False
    assert alias_gate.allowed is True
    assert canonical_action.allowed is True
    assert direct_alias.allowed is False


def test_high_risk_capability_action_requires_gate():
    config = BuiltinAgentConfig(permission_mode="workspace_write")
    decision = check_tool_execution(
        skill_name="invoices",
        args={"action": "approve"},
        config=config,
        approval_gates=set(),
    )

    assert decision.allowed is False
    assert decision.required_approval is True
    assert decision.risk_level == "high"
    assert "invoices.approve" in decision.reason


def test_high_risk_capability_action_is_allowed_to_request_approval_when_gated():
    config = BuiltinAgentConfig(permission_mode="workspace_write")
    decision = check_tool_execution(
        skill_name="invoices",
        args={"action": "approve"},
        config=config,
        approval_gates={"invoices"},
    )

    assert decision.allowed is True
    assert decision.risk_level == "high"
