"""Role-regression eval for the agent's capability-dispatch contract.

This replaces the old WebSocket/registry eval (test_agent_role_websocket.py),
which modelled the pre-dispatcher per-skill protocol and the old WS framing.
Here we drive the REAL agent engine (`agent_loop.AgentSession`) in the current
production **capabilities mode** with a scripted LLM, and verify the durable
contract for each department role:

1. **Availability** — every (capability, action) the role needs is a real,
   exposed capability action (catches fixture drift / removed actions — the
   exact class of bug that silently broke the old eval).
2. **Dispatch fidelity** — the agent loop routes each scripted tool call to
   `_execute_skill` with the correct capability + action, in order.
3. **Gate correctness** — actions declared as gate_actions in capabilities.yml
   trigger an approval request; non-gated actions do not.
4. **Turn completion** — the turn emits assistant text and a final ``done``.

The LLM is scripted (we don't test model planning — that needs a live model),
so this is an engine/contract regression, exactly like the eval it replaces,
but aligned with the 15-capability dispatcher instead of the legacy registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig
from app.api.capability_router import _DISPATCH

MANIFEST_PATH = Path(__file__).parent.parent.parent / "app/ai/evals/agent_role_cases.json"


def _load_roles() -> list[dict]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["roles"]


def _gate_actions() -> dict[str, set[str]]:
    """capability → set(gate actions), read from the live capabilities.yml."""
    import yaml

    from app.ai.gateway_config import gateway_config

    data = yaml.safe_load(gateway_config.capabilities_path.read_text(encoding="utf-8"))
    return {c["name"]: set(c.get("gate_actions") or []) for c in data.get("capabilities", [])}


@pytest.mark.asyncio
@pytest.mark.parametrize("role_case", _load_roles(), ids=lambda r: r["id"])
async def test_role_capability_dispatch(role_case, monkeypatch):
    dispatch: list[list[str]] = role_case["expected_dispatch"]
    user_request: str = role_case["user_request"]

    # 1) Availability — each (capability, action) must exist in the dispatcher.
    for cap, action in dispatch:
        assert cap in _DISPATCH, f"unknown capability '{cap}'"
        assert action in _DISPATCH[cap], f"unknown action '{cap}.{action}'"

    gates = _gate_actions()
    expected_gates = [
        [cap, action] for cap, action in dispatch if action in gates.get(cap, set())
    ]

    config = BuiltinAgentConfig(
        enabled=True,
        agent_name="Света",
        model="mock-role-runner",
        provider="ollama",
        backend_url="http://backend",
        ollama_url="http://ollama",
        memory_enabled=False,
        audit_enabled=False,
        context_compression_enabled=False,
        max_steps=len(dispatch) + 3,
    )
    monkeypatch.setattr(agent_loop, "get_builtin_agent_config", lambda: config)

    # Silence side-effecting helpers (memory / MCP / audit / HTTP).
    async def _noop(*a, **k):
        return None

    for name in (
        "_log_action", "_init_mcp", "_append_memory_context",
        "_inject_rating_hint", "_inject_learning_rules", "_remember_latest_turn",
    ):
        monkeypatch.setattr(agent_loop.AgentSession, name, _noop, raising=False)

    # Record real dispatches (capability, action) routed through _execute_skill.
    dispatched: list[list[str]] = []

    async def fake_execute_skill(skill, args, config):  # noqa: A002
        dispatched.append([skill["name"], args.get("action")])
        return {"status": "ok"}

    monkeypatch.setattr(agent_loop, "_execute_skill", fake_execute_skill)

    # Record gate triggers (capability, action) and auto-approve.
    triggered_gates: list[list[str]] = []

    async def fake_request_approval(self, skill_name, args):
        triggered_gates.append([skill_name, args.get("action")])
        return True

    monkeypatch.setattr(agent_loop.AgentSession, "_request_approval", fake_request_approval)

    # Scripted LLM: emit one capability tool-call per step, then a final answer.
    step = {"i": 0}

    async def fake_call_provider_streaming(
        messages, tools, system_prompt, config, on_token,
        model_override=None, provider_override=None,
        disable_thinking_override=None, on_thinking=None, **kwargs,
    ):
        i = step["i"]
        step["i"] += 1
        if i < len(dispatch):
            cap, action = dispatch[i]
            return {
                "tool_calls": [{
                    "type": "function",
                    "function": {"name": cap, "arguments": {"action": action}},
                }],
            }
        await on_token("Готово.")
        return {"tool_calls": None}

    monkeypatch.setattr(agent_loop, "_call_provider_streaming", fake_call_provider_streaming)

    events: list[dict] = []

    async def collect(evt: dict):
        events.append(evt)

    session = agent_loop.AgentSession(collect)
    await session.on_user_message(user_request)

    # 2) Dispatch fidelity — exact capability+action sequence, in order.
    assert dispatched == dispatch, (
        f"{role_case['id']}: dispatched {dispatched} != expected {dispatch}"
    )

    # 3) Gate correctness — gated actions requested approval; nothing extra.
    assert triggered_gates == expected_gates, (
        f"{role_case['id']}: gates {triggered_gates} != expected {expected_gates}"
    )

    # 4) Turn completion — assistant text + a final done frame.
    types = [e.get("type") for e in events]
    assert "text" in types, f"no assistant text emitted: {types}"
    assert "done" in types, f"no done frame emitted: {types}"
