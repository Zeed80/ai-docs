"""Defaulted-router rescue: a degenerate TurnDecision (specialist/0.0/[] — the
model ignored the JSON schema) must NOT be dispatched as a blind chat
specialist. An obvious table request is rescued to analytical_table; anything
else degrades to the heuristic planner (route_unavailable)."""

from __future__ import annotations

import pytest

from app.ai import orchestrator as orch
from app.ai import turn_router
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.orchestrator import AgentOrchestrator


def _config() -> BuiltinAgentConfig:
    return BuiltinAgentConfig(
        enabled=True, model="gemma4:31b", worker_model="gemma4:31b",
        orchestrator_model="gemma4:31b", fast_model="gemma4:31b",
        backend_url="http://b", ollama_url="http://o",
    )


def _orch():
    async def _send(_m):
        return None
    return AgentOrchestrator(_send)


@pytest.mark.asyncio
async def test_defaulted_router_rescued_for_table_request(monkeypatch):
    async def fake_route(content, **kw):
        return turn_router.safe_default_decision(content), "model"  # degenerate shell
    monkeypatch.setattr(turn_router, "route_turn", fake_route)

    sess = _orch()
    decision = await sess._decide_turn("выведи все фрезы по поставщику", _config())
    assert decision.intent == "analytical_table"
    assert decision.grounding == "structured"
    assert decision.output_channel == "workspace"
    assert sess._route_unavailable is False


@pytest.mark.asyncio
async def test_defaulted_router_degrades_for_non_table(monkeypatch):
    async def fake_route(content, **kw):
        return turn_router.safe_default_decision(content), "model"
    monkeypatch.setattr(turn_router, "route_turn", fake_route)

    sess = _orch()
    await sess._decide_turn("расскажи анекдот про инженера", _config())
    # Not a table request → treated as unavailable → heuristic planner downstream.
    assert sess._route_unavailable is True
