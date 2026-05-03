from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig
from app.main import app

MANIFEST_PATH = Path("app/ai/evals/agent_role_cases.json")
REGISTRY_PATH = Path("/aiagent/skills/_registry.yml")


def _load_roles() -> list[dict]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["roles"]


@pytest.mark.parametrize("role_case", _load_roles(), ids=lambda role: role["id"])
def test_role_regression_runs_multi_step_agent_turn_over_websocket(
    role_case: dict,
    monkeypatch,
) -> None:
    expected_tools = role_case["expected_tool_sequence"]
    approval_gates = set(role_case["expected_approval_gates"])
    terms = role_case["expected_terms"]
    config = BuiltinAgentConfig(
        model="mock-role-runner",
        backend_url="http://backend",
        ollama_url="http://ollama",
        exposed_skills=expected_tools,
        approval_gates=sorted(approval_gates),
        memory_enabled=False,
        max_steps=len(expected_tools) + 2,
    )
    state = {"step": 0}
    executed_tools: list[str] = []

    monkeypatch.setattr(agent_loop, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(
        agent_loop,
        "gateway_config",
        SimpleNamespace(registry_path=REGISTRY_PATH, base_prompt_path=Path("/missing")),
    )
    monkeypatch.setattr(agent_loop.AgentSession, "_log_action", _noop_log_action)
    monkeypatch.setattr(agent_loop, "_create_db_approval", _fake_create_db_approval)
    monkeypatch.setattr(agent_loop, "_decide_db_approval", _fake_decide_db_approval)

    async def fake_call_ollama_streaming(
        messages,
        tools,
        system_prompt,
        runtime_config,
        on_token,
    ):
        available_tool_names = {tool["function"]["name"] for tool in tools}
        assert {_sanitize(name) for name in expected_tools}.issubset(available_tool_names)
        step = state["step"]
        if step < len(expected_tools):
            tool_name = expected_tools[step]
            state["step"] += 1
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": _sanitize(tool_name),
                            "arguments": _arguments_for(tool_name),
                        }
                    }
                ],
            }

        final_text = (
            f"{role_case['profession']}: выполнен маршрут. "
            + "; ".join(terms)
        )
        await on_token(final_text)
        return {"role": "assistant", "content": final_text}

    async def fake_execute_skill(skill, args, runtime_config):
        executed_tools.append(skill["name"])
        return {"ok": True, "tool": skill["name"], "args": args}

    monkeypatch.setattr(agent_loop, "_call_ollama_streaming", fake_call_ollama_streaming)
    monkeypatch.setattr(agent_loop, "_execute_skill", fake_execute_skill)

    received: list[dict] = []
    with TestClient(app) as client:
        with client.websocket_connect("/ws/chat") as websocket:
            assert websocket.receive_json()["type"] == "text"
            assert websocket.receive_json() == {"type": "done"}
            websocket.send_json({"type": "message", "content": role_case["user_request"]})
            while True:
                event = websocket.receive_json()
                received.append(event)
                if event["type"] == "approval_request":
                    assert event["tool"] in approval_gates
                    websocket.send_json({"type": "approve"})
                if event["type"] == "done":
                    break

    tool_calls = [
        event["tool"].replace("__", ".")
        for event in received
        if event["type"] == "tool_call"
    ]
    approvals = [
        event["tool"]
        for event in received
        if event["type"] == "approval_request"
    ]
    final_text = "".join(
        event["content"]
        for event in received
        if event["type"] == "text"
    )

    assert tool_calls == expected_tools
    assert executed_tools == expected_tools
    assert approvals == role_case["expected_approval_gates"]
    for term in terms:
        assert term in final_text


def _sanitize(name: str) -> str:
    return name.replace(".", "__")


def _arguments_for(tool_name: str) -> dict:
    common_id = "11111111-1111-1111-1111-111111111111"
    if tool_name.startswith("memory."):
        return {"query": "ГОСТ Сталь 40Х", "limit": 5}
    if tool_name.startswith("doc.") or tool_name.startswith("ntd.check"):
        return {"document_id": common_id}
    if tool_name.startswith("graph."):
        return {"node_id": common_id}
    if tool_name.startswith("tech.process_plan"):
        return {"plan_id": common_id}
    if tool_name.startswith("tech.norm_estimate"):
        return {"estimate_id": common_id}
    if tool_name.startswith("tech.learning_rule"):
        return {"rule_id": common_id}
    if tool_name.startswith("warehouse.") and "receipt" in tool_name:
        return {"receipt_id": common_id}
    if tool_name.startswith("bom."):
        return {"bom_id": common_id}
    if tool_name.startswith("procurement."):
        return {"request_id": common_id}
    if tool_name.startswith("payment."):
        return {"invoice_id": common_id, "schedule_id": common_id}
    if tool_name.startswith("supplier."):
        return {"query": "поставщик", "supplier_id": common_id}
    if tool_name.startswith("email."):
        return {"to": "supplier@example.test", "subject": "Запрос уточнений"}
    return {"entity_id": common_id}


async def _noop_log_action(self, **kwargs):
    return None


async def _fake_create_db_approval(skill_name: str, args: dict) -> str:
    return "22222222-2222-2222-2222-222222222222"


async def _fake_decide_db_approval(approval_id: str, approved: bool) -> None:
    return None
