"""Cloud routing policy: configurable cloud planner/auditor, confidential stays local."""

from __future__ import annotations

import pytest

from app.ai import orchestrator as orchestrator_module
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.model_tier import Tier
from app.ai.orchestrator import AgentOrchestrator, AuditReport
from app.ai.policy_engine import PROTECTED_SETTINGS, is_protected_setting
from app.ai.router import AIConfidentialityPolicyError, ai_router
from app.ai.schemas import AIRequest, AITask, ChatMessage


def _cloud_model_name() -> str | None:
    """A registered cloud (anthropic) model from the live registry, if any."""
    for name, model in ai_router.registry.models.items():
        if model.provider == "anthropic":
            return name
    return None


def test_registry_has_anthropic_models():
    assert _cloud_model_name() is not None, (
        "model_registry.yaml must register at least one anthropic model "
        "for the configurable-cloud mode"
    )


@pytest.mark.asyncio
async def test_confidential_request_never_routes_to_cloud():
    """The router hard-blocks confidential content from any cloud model."""
    cloud = _cloud_model_name()
    assert cloud
    model = ai_router.registry.models[cloud]
    request = AIRequest(
        task=AITask.CLASSIFICATION,
        messages=[ChatMessage(role="user", content="секретные данные")],
        confidential=True,
        allow_cloud=True,  # even with cloud allowed, confidential wins
        preferred_model=cloud,
    )
    with pytest.raises(AIConfidentialityPolicyError):
        ai_router._enforce_policy(request, model)


@pytest.mark.asyncio
async def test_cloud_model_requires_allow_cloud():
    cloud = _cloud_model_name()
    assert cloud
    model = ai_router.registry.models[cloud]
    request = AIRequest(
        task=AITask.CLASSIFICATION,
        messages=[ChatMessage(role="user", content="обычные данные")],
        confidential=False,
        allow_cloud=False,
        preferred_model=cloud,
    )
    with pytest.raises(AIConfidentialityPolicyError):
        ai_router._enforce_policy(request, model)


def test_auditor_allow_cloud_is_protected_and_off_by_default():
    config = BuiltinAgentConfig(
        model="mock", backend_url="http://backend", ollama_url="http://ollama",
        exposed_skills=[],
    )
    assert config.auditor_allow_cloud is False
    assert "auditor_allow_cloud" in PROTECTED_SETTINGS
    assert is_protected_setting("auditor_allow_cloud")


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud_enabled", [False, True])
async def test_semantic_audit_propagates_auditor_allow_cloud(monkeypatch, cloud_enabled):
    config = BuiltinAgentConfig(
        department_enabled=True, audit_enabled=True,
        auditor_allow_cloud=cloud_enabled,
        model="mock", backend_url="http://backend", ollama_url="http://ollama",
        exposed_skills=[],
    )
    monkeypatch.setattr(orchestrator_module, "get_builtin_agent_config", lambda: config)

    seen: list[AIRequest] = []

    async def capture_run(request, *a, **k):
        seen.append(request)
        raise RuntimeError("stop here")

    monkeypatch.setattr(orchestrator_module.ai_router, "run", capture_run)

    async def _noop(_msg):
        return None

    session = AgentOrchestrator(_noop)
    session._tier = Tier.EXPERT
    session._trace.text_chunks.append("ответ для проверки")
    report = AuditReport(passed=True, issues=[])

    from app.ai.orchestrator import OrchestratorPlan, WorkerAssignment, WorkspaceOutputSpec
    plan = OrchestratorPlan(
        goal="тест", intent="general",
        worker=WorkerAssignment(role="data_analyst", task="тест"),
        workspace=WorkspaceOutputSpec(),
    )
    await session._run_semantic_audit(plan, config, report)

    assert seen, "semantic audit must call the router"
    assert seen[0].confidential is False
    assert seen[0].allow_cloud is cloud_enabled
