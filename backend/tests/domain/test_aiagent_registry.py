from __future__ import annotations

import json

import yaml
from scripts.check_aiagent_contract import check_contract
from scripts.generate_aiagent_official_sample import build_official_sample
from scripts.generate_aiagent_registry import build_registry
from scripts.generate_aiagent_strict_gateway import build_strict_gateway

from app.api.aiagent_gateway import (
    AiAgentProjectSettings,
    _build_official_config,
    _registry_tool,
)
from app.db.models import ApprovalActionType
from app.domain.aiagent_gateway import (
    AiAgentApprovalRequest,
    AiAgentApprovalTicket,
    AiAgentResumeStatus,
)


def test_aiagent_registry_is_generated_from_openapi() -> None:
    registry = build_registry()
    tools = {tool["name"]: tool for tool in registry["tools"]}

    assert registry["source"] == "fastapi_openapi"
    assert tools["doc.extract"]["response_schema"] == "#/components/schemas/TaskResponse"
    assert tools["email.send"]["approval_required"] is True
    assert tools["invoice.export.1c.prepare"]["approval_required"] is True
    assert tools["memory.reindex"]["approval_required"] is False
    assert tools["graph.review_list"]["approval_required"] is False
    assert tools["tech.process_plan_draft_from_document"]["request_schema"] == (
        "#/components/schemas/ProcessPlanDraftFromDocumentRequest"
    )
    assert tools["tech.process_plan_approve"]["approval_required"] is True


def test_aiagent_registry_can_be_serialized_as_json_and_yaml() -> None:
    registry = build_registry()
    as_json = json.loads(json.dumps(registry, ensure_ascii=False))
    as_yaml = yaml.safe_load(yaml.safe_dump(registry, allow_unicode=True, sort_keys=False))

    assert as_json["tools"] == as_yaml["tools"]
    assert any(tool["name"] == "tech.learning_rule_activate" for tool in as_yaml["tools"])


def test_aiagent_gateway_registry_file_is_current_yaml() -> None:
    raw = yaml.safe_load(open("aiagent/skills/_registry.yml", encoding="utf-8"))
    tools = {tool["name"]: tool for tool in raw["tools"]}

    assert raw["source"] == "fastapi_openapi"
    assert tools["tech.learning_rule_activate"]["approval_required"] is True
    assert tools["memory.reindex"]["approval_required"] is False


def test_aiagent_contract_has_no_generated_policy_errors() -> None:
    result = check_contract()

    assert result["ok"] is True
    assert result["registry_tools"] >= 40
    assert "tech.learning_rule_activate" in {
        tool["name"] for tool in build_registry()["tools"]
    }


def test_aiagent_strict_gateway_only_exposes_generated_tools() -> None:
    strict_gateway = build_strict_gateway()
    registry_tools = {tool["name"]: tool for tool in build_registry()["tools"]}

    assert set(strict_gateway["skills"]["exposed"]) == set(registry_tools)
    assert set(strict_gateway["skills"]["approval_gates"]) == {
        name for name, tool in registry_tools.items() if tool["approval_required"]
    }
    assert {scenario["name"] for scenario in strict_gateway["scenarios"]} == {
        "assisted_review",
        "memory_maintenance",
    }


def test_aiagent_gateway_control_api_uses_generated_approval_policy() -> None:
    tool = _registry_tool("tech.process_plan_approve")

    assert tool is not None
    assert tool["approval_required"] is True
    assert ApprovalActionType.agent_tool_call.value == "agent.tool_call"


def test_aiagent_official_sample_uses_strict_skill_allowlist() -> None:
    sample = build_official_sample()
    strict_gateway = build_strict_gateway()

    assert sample["agents"]["defaults"]["skills"] == strict_gateway["skills"]["exposed"]
    assert sample["session"]["dmScope"] == "per-channel-peer"
    assert sample["gateway"]["auth"]["token"] == "${AIAGENT_GATEWAY_TOKEN}"


def test_aiagent_project_settings_build_first_run_official_config() -> None:
    settings = AiAgentProjectSettings(
        first_run_completed=False,
        gateway_auth="token",
        gateway_token_configured=True,
        model_primary="openai/gpt-5.5",
        model_fallbacks=["local/qwen3.6:35b"],
        model_allowlist=["openai/gpt-5.5", "local/qwen3.6:35b"],
        telegram_enabled=True,
        telegram_bot_token_configured=True,
        telegram_dm_policy="allowlist",
        telegram_allow_from=["123456"],
    )

    config, warnings = _build_official_config(settings)

    assert warnings == []
    assert config["gateway"]["auth"] == {
        "mode": "token",
        "token": "${AIAGENT_GATEWAY_TOKEN}",
    }
    assert config["agents"]["defaults"]["model"]["primary"] == "openai/gpt-5.5"
    assert config["agents"]["defaults"]["model"]["fallbacks"] == ["local/qwen3.6:35b"]
    assert config["channels"]["telegram"]["botToken"] == "${TELEGRAM_BOT_TOKEN}"
    assert config["channels"]["telegram"]["allowFrom"] == ["123456"]


def test_aiagent_gateway_pause_resume_callback_schemas_match_gateway_contract() -> None:
    request = AiAgentApprovalRequest(
        session_id="official-gateway-smoke",
        iteration=1,
        tool_name="tech.process_plan_approve",
        tool_args={"process_plan_id": "plan-1"},
        reason="Official Gateway approval gate smoke test",
    )
    ticket = AiAgentApprovalTicket(
        approval_id="00000000-0000-0000-0000-000000000001",
        agent_action_id="00000000-0000-0000-0000-000000000002",
        status="pending",
        tool_name=request.tool_name,
        created_at="2026-04-28T20:00:00Z",
    )
    resume = AiAgentResumeStatus(
        approval_id=ticket.approval_id,
        status="approved",
        approved=True,
        rejected=False,
        tool_name=request.tool_name,
        tool_args=request.tool_args,
        decided_by="tester",
        decided_at="2026-04-28T20:01:00Z",
    )

    assert request.tool_name == "tech.process_plan_approve"
    assert ticket.status == "pending"
    assert resume.approved is True
    assert resume.tool_args == {"process_plan_id": "plan-1"}
