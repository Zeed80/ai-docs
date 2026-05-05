from types import SimpleNamespace

import pytest

from app.ai import agent_config as agent_config_module
from app.ai import agent_loop
from app.ai.agent_config import (
    BuiltinAgentConfig,
    BuiltinAgentConfigUpdate,
    get_builtin_agent_config,
    update_builtin_agent_config,
)


def test_builtin_agent_config_persists_runtime_settings(tmp_path, monkeypatch):
    config_file = tmp_path / "agent_config.json"
    monkeypatch.setattr(agent_config_module, "_CONFIG_FILE", config_file)
    monkeypatch.setattr(
        agent_config_module,
        "gateway_config",
        SimpleNamespace(
            agent_name="Света",
            reasoning_model="qwen3.5:9b",
            reasoning_base_url="http://ollama:11434",
            backend_url="http://backend:8000",
            backend_timeout=30,
            exposed_skills={"memory.search", "doc.list"},
            approval_gates={"email.send"},
        ),
    )

    config = update_builtin_agent_config(
        BuiltinAgentConfigUpdate(
            model="qwen3.6:35b",
            disable_thinking=True,
            exposed_skills=["memory.search"],
        )
    )

    assert config.model == "qwen3.6:35b"
    assert config.disable_thinking is True
    assert get_builtin_agent_config().exposed_skills == ["memory.search"]


def test_agent_registry_loader_accepts_tools_key(tmp_path, monkeypatch):
    registry_path = tmp_path / "_registry.yml"
    registry_path.write_text(
        """
tools:
  - name: memory.search
    description: Search memory.
    method: POST
    path: /api/memory/search
  - name: email.send
    description: Send email.
    method: POST
    path: /api/email/drafts/{draft_id}/send
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent_loop,
        "gateway_config",
        SimpleNamespace(registry_path=registry_path),
    )

    tools, skill_map = agent_loop._load_registry(expose_filter={"memory.search"})

    assert [tool["function"]["name"] for tool in tools] == ["memory__search"]
    assert skill_map["memory__search"]["path"] == "/api/memory/search"


def test_agent_approval_map_covers_high_risk_tools():
    high_risk_tools = {
        "invoice.bulk_delete": "invoice.bulk_delete",
        "warehouse.confirm_receipt": "warehouse.confirm_receipt",
        "payment.mark_paid": "payment.mark_paid",
        "procurement.send_rfq": "procurement.send_rfq",
        "bom.approve": "bom.approve",
        "bom.create_purchase_request": "bom.create_purchase_request",
        "tech.process_plan_approve": "tech.process_plan_approve",
        "tech.norm_estimate_approve": "tech.norm_estimate_approve",
        "tech.learning_rule_activate": "tech.learning_rule_activate",
    }

    for skill_name, action_type in high_risk_tools.items():
        assert agent_loop._APPROVAL_ACTION_TYPE_MAP[skill_name] == action_type


@pytest.mark.asyncio
async def test_builtin_agent_chat_turn_executes_mock_tool(tmp_path, monkeypatch):
    registry_path = tmp_path / "_registry.yml"
    registry_path.write_text(
        """
tools:
  - name: memory.search
    description: Search memory.
    method: POST
    path: /api/memory/search
    parameters:
      properties:
        query:
          type: string
      required:
        - query
""",
        encoding="utf-8",
    )
    config = BuiltinAgentConfig(
        model="mock-model",
        ollama_url="http://ollama",
        backend_url="http://backend",
        exposed_skills=["memory.search"],
        approval_gates=[],
        memory_enabled=False,
        max_steps=3,
    )
    sent: list[dict] = []
    call_count = 0

    monkeypatch.setattr(agent_loop, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(
        agent_loop,
        "gateway_config",
        SimpleNamespace(registry_path=registry_path, base_prompt_path=tmp_path / "base.md"),
    )
    monkeypatch.setattr(agent_loop.AgentSession, "_log_action", _noop_log_action)

    async def fake_call_ollama_streaming(
        messages,
        tools,
        system_prompt,
        runtime_config,
        on_token,
    ):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            assert runtime_config.model == "mock-model"
            assert tools[0]["function"]["name"] == "memory__search"
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "memory__search",
                            "arguments": {"query": "ГОСТ", "limit": 1},
                        }
                    }
                ],
            }
        await on_token("Готово")
        return {"role": "assistant", "content": "Готово"}

    async def fake_execute_skill(skill, args, runtime_config):
        assert skill["name"] == "memory.search"
        assert args["query"] == "ГОСТ"
        return {"hits": []}

    monkeypatch.setattr(agent_loop, "_call_ollama_streaming", fake_call_ollama_streaming)
    monkeypatch.setattr(agent_loop, "_execute_skill", fake_execute_skill)

    async def capture(message: dict):
        sent.append(message)

    session = agent_loop.AgentSession(send=capture)
    await session.on_user_message("Найди требования ГОСТ")

    assert call_count == 2
    assert {
        "type": "tool_call",
        "tool": "memory__search",
        "args": {"query": "ГОСТ", "limit": 1},
    } in sent
    assert {"type": "tool_result", "tool": "memory__search", "result": {"hits": []}} in sent
    assert {"type": "text", "content": "Готово"} in sent
    assert sent[-1] == {"type": "done"}


async def _noop_log_action(self, **kwargs):
    return None


@pytest.mark.asyncio
async def test_agent_retries_on_empty_response_after_tool_result(tmp_path, monkeypatch):
    registry_path = tmp_path / "_registry.yml"
    registry_path.write_text(
        """
tools:
  - name: invoice.list
    description: List invoices.
    method: GET
    path: /api/invoices
""",
        encoding="utf-8",
    )
    config = BuiltinAgentConfig(
        model="mock-model",
        ollama_url="http://ollama",
        backend_url="http://backend",
        exposed_skills=["invoice.list"],
        approval_gates=[],
        memory_enabled=False,
        max_steps=4,
    )
    sent: list[dict] = []
    call_count = 0

    monkeypatch.setattr(agent_loop, "get_builtin_agent_config", lambda: config)
    monkeypatch.setattr(
        agent_loop,
        "gateway_config",
        SimpleNamespace(registry_path=registry_path, base_prompt_path=tmp_path / "base.md"),
    )
    monkeypatch.setattr(agent_loop.AgentSession, "_log_action", _noop_log_action)

    async def fake_call_provider_streaming(
        messages,
        tools,
        system_prompt,
        runtime_config,
        on_token,
        model_override=None,
    ):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "invoice__list", "arguments": {}}}],
            }
        if call_count == 2:
            # Reproduces the bug: empty assistant response after tool result.
            return {"role": "assistant", "content": ""}
        await on_token("Готово, анализ завершен")
        return {"role": "assistant", "content": "Готово, анализ завершен"}

    async def fake_execute_skill(skill, args, runtime_config):
        assert skill["name"] == "invoice.list"
        return {"items": [{"id": "inv-1"}], "total": 1}

    monkeypatch.setattr(agent_loop, "_call_provider_streaming", fake_call_provider_streaming)
    monkeypatch.setattr(agent_loop, "_execute_skill", fake_execute_skill)

    async def capture(message: dict):
        sent.append(message)

    session = agent_loop.AgentSession(send=capture)
    await session.on_user_message("Построй связи по всем счетам")

    assert call_count == 3
    assert {"type": "text", "content": "Готово, анализ завершен"} in sent
    assert sent[-1] == {"type": "done"}
