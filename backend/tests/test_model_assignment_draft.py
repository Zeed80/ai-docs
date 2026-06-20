import pytest
from httpx import AsyncClient

from app.ai import task_routing as tr
from app.ai.schemas import AITask, ModelCapability, ModelStatus, Modality, ProviderKind
from app.api.providers_api import _verification_warning


@pytest.fixture
def routing_mem_store(monkeypatch):
    store: dict[str, dict] = {}
    monkeypatch.setattr(tr, "_redis_get", lambda: dict(store) if store else None)

    def _set(value):
        store.clear()
        store.update(value)

    monkeypatch.setattr(tr, "_redis_set", _set)
    return store


def test_production_manual_capability_is_not_reported_as_failed_verification():
    cap = ModelCapability(
        name="prod_manual",
        provider=ProviderKind.OLLAMA,
        provider_model="prod-manual:latest",
        status=ModelStatus.PRODUCTION,
        modalities={Modality.TEXT},
        capability_source="manual",
    )

    assert _verification_warning("agent_fast", "prod_manual", cap) is None


def test_discovered_capability_gets_precise_unverified_profile_warning():
    cap = ModelCapability(
        name="live_discovered",
        provider=ProviderKind.OLLAMA,
        provider_model="live:latest",
        status=ModelStatus.CANDIDATE,
        modalities={Modality.TEXT},
        capability_source="discovered",
    )

    issue = _verification_warning("agent_fast", "live_discovered", cap)

    assert issue is not None
    assert issue.code == "not_production"
    assert "не прошла" not in issue.message


@pytest.mark.asyncio
async def test_assignment_draft_validate_does_not_apply(
    client: AsyncClient,
    monkeypatch,
    routing_mem_store,
):
    async def _loaded_index():
        return {("ollama", "qwen3.5:9b"): "test-node", ("ollama", "qwen3.5"): "test-node"}

    monkeypatch.setattr("app.api.providers_api._loaded_index", _loaded_index)

    before = tr.get_routing_for(AITask.STRUCTURED_EXTRACTION).primary
    resp = await client.post(
        "/api/providers/assignment-draft/validate",
        json={"slots": {"structured_extraction": "qwen3_5_9b_ollama"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["diff"]
    assert tr.get_routing_for(AITask.STRUCTURED_EXTRACTION).primary == before


@pytest.mark.asyncio
async def test_assignment_draft_apply_and_rollback(
    client: AsyncClient,
    monkeypatch,
    routing_mem_store,
):
    async def _loaded_index():
        return {("ollama", "qwen3.5:9b"): "test-node", ("ollama", "qwen3.5"): "test-node"}

    monkeypatch.setattr("app.api.providers_api._loaded_index", _loaded_index)

    before = tr.get_routing_for(AITask.STRUCTURED_EXTRACTION).primary
    apply_resp = await client.post(
        "/api/providers/assignment-draft/apply",
        json={
            "slots": {"structured_extraction": "qwen3_5_9b_ollama"},
            "confirm_warnings": True,
        },
    )
    assert apply_resp.status_code == 200
    applied = apply_resp.json()
    assert applied["revision_id"]
    assert tr.get_routing_for(AITask.STRUCTURED_EXTRACTION).primary == "qwen3_5_9b_ollama"

    # Rollback now gates warnings symmetrically with apply (Fix 8): the target
    # model may carry a non-blocking warning, so confirm_warnings is required.
    rollback_resp = await client.post(
        f"/api/providers/assignments/{applied['revision_id']}/rollback",
        params={"confirm_warnings": "true"},
    )
    assert rollback_resp.status_code == 200
    assert tr.get_routing_for(AITask.STRUCTURED_EXTRACTION).primary == before
