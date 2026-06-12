from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

try:
    from scripts.check_aiagent_contract import check_contract
    from scripts.generate_aiagent_registry import build_registry
except ImportError:
    import pytest
    pytest.skip(
        "scripts.generate_aiagent_registry not yet implemented",
        allow_module_level=True,
    )

import json

import yaml

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


def test_aiagent_contract_has_no_generated_policy_errors() -> None:
    result = check_contract()

    assert result["ok"] is True
    assert result["registry_tools"] >= 40
    assert "tech.learning_rule_activate" in {
        tool["name"] for tool in build_registry()["tools"]
    }
