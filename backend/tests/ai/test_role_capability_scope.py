"""Role scoping: a worker's visible tool set is limited to its capability allowlist."""

from __future__ import annotations

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.gateway_config import gateway_config


def _tool_names(tools: list[dict]) -> set[str]:
    names = set()
    for tool in tools:
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        names.add(str(fn.get("name") or ""))
    return names


@pytest.fixture()
def session(monkeypatch):
    config = BuiltinAgentConfig(
        enabled=True,
        model="mock",
        backend_url="http://backend",
        ollama_url="http://ollama",
        memory_enabled=False,
        exposed_skills=[],
    )
    monkeypatch.setattr(agent_loop, "get_builtin_agent_config", lambda: config)

    async def _send(_msg):
        return None

    return agent_loop.AgentSession(_send)


def test_gateway_declares_role_capabilities():
    caps = gateway_config.role_capabilities("technologist")
    assert "tech" in caps and "documents" in caps
    # Legacy string entries must still work as prompt-only roles.
    assert gateway_config.role_capabilities("nonexistent_role") == []


def test_role_scoping_filters_tools(session):
    if gateway_config.skills_mode != "capabilities":
        pytest.skip("role scoping applies in capabilities mode only")
    all_names = _tool_names(session._tools)
    assert "invoices" in all_names  # sanity: full set before scoping

    session.set_active_role("technologist")
    scoped = _tool_names(session._tools_for_turn())

    allowed = set(gateway_config.role_capabilities("technologist"))
    core = set(agent_loop.AgentSession._CORE_CAPABILITIES)
    # Everything visible is either allowed, core, or not a registry capability.
    registry_caps = set(agent_loop._load_capabilities()[1].keys())
    for name in scoped:
        if name in registry_caps:
            assert name in allowed | core, f"{name} leaked into technologist scope"
    # The blocked capabilities are actually gone.
    assert "invoices" not in scoped
    assert "email" not in scoped
    # Core capabilities survive scoping.
    assert "workspace" in scoped
    assert "memory" in scoped
    assert "search" in scoped
    # Allowed capabilities survive scoping.
    assert "tech" in scoped
    assert "documents" in scoped


def test_role_without_allowlist_sees_full_set(session):
    session.set_active_role("nonexistent_role")
    assert _tool_names(session._tools_for_turn()) == _tool_names(session._tools)


def test_no_role_sees_full_set(session):
    session.set_active_role(None)
    assert _tool_names(session._tools_for_turn()) == _tool_names(session._tools)
