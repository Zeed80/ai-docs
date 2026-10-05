"""The turn router asks the model for every decision field (live 2026-10-05)."""

import pytest

from app.ai import turn_router
from app.ai.schemas import AIResponse, AITask, ProviderKind


@pytest.mark.asyncio
async def test_route_turn_requests_a_schema_with_every_field_required(monkeypatch):
    seen = []

    async def fake_run(request):
        seen.append(request)
        decision = turn_router.TurnDecision(
            intent="document_op",
            role="procurement_specialist",
            recommended=[{"capability": "email", "action": ""}],
            confidence=0.9,
        )
        return AIResponse(
            task=AITask.ORCHESTRATOR_PLANNING,
            provider=ProviderKind.OLLAMA,
            model="m",
            text="{}",
            data=decision,
        )

    monkeypatch.setattr("app.ai.router.ai_router.run", fake_run)
    decision, source = await turn_router.route_turn(
        "Подготовь черновик письма поставщику", preferred_model=None, timeout=5
    )

    assert source == "model"
    assert decision is not None and decision.role == "procurement_specialist"
    schema = seen[0].metadata["json_schema"]
    assert set(schema["required"]) == set(turn_router.TurnDecision.model_fields)
    item = schema["properties"]["recommended"]["items"]
    assert item["required"] == ["capability", "action"]
    # Free-form entities stay a string map, not a closed empty object.
    assert schema["properties"]["entities"]["additionalProperties"] == {"type": "string"}


def test_defaults_still_validate_when_a_model_omits_fields():
    decision = turn_router.TurnDecision.model_validate({"intent": "count"})
    assert decision.role == "data_analyst"
    assert decision.confidence == 0.0
