from app.ai.agent_config import BuiltinAgentConfig
from app.ai.policy_engine import check_tool_execution, is_protected_setting


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

