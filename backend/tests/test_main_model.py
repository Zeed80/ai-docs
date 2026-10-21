"""«Основная модель»: one model for every slot that follows it."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.ai import agent_config as ac
from app.ai import task_routing as tr
from app.ai.schemas import AITask
from app.api import providers_api as pa


@pytest.fixture
def routing_mem_store(monkeypatch, tmp_path):
    """Task routing and the agent config in memory, never the stand's stores."""
    store: dict[str, dict] = {}
    monkeypatch.setattr(tr, "_redis_get", lambda: dict(store) if store else None)

    def _set(value):
        store.clear()
        store.update(value)

    monkeypatch.setattr(tr, "_redis_set", _set)
    agent_store: dict = {}
    monkeypatch.setattr(
        ac, "_redis_get_agent_config", lambda: dict(agent_store) if agent_store else None
    )
    monkeypatch.setattr(ac, "_redis_set_agent_config", lambda data: agent_store.update(data))
    monkeypatch.setattr(ac, "_CONFIG_FILE", tmp_path / "agent_config.json")
    return store


@pytest.fixture
def loaded(monkeypatch):
    async def _loaded_index():
        return {
            ("ollama", "qwen3.5:9b"): "test-node",
            ("ollama", "qwen3.5"): "test-node",
            ("ollama", "gemma4:e4b"): "test-node",
        }

    monkeypatch.setattr("app.api.providers_api._loaded_index", _loaded_index)


@pytest.mark.asyncio
async def test_following_slots_take_the_main_model(client: AsyncClient, routing_mem_store, loaded):
    resp = await client.post(
        "/api/providers/assignment-draft/apply",
        json={
            "main_model": "gemma4_e4b_ollama",
            "slots": {
                "structured_extraction": {"follows_main": True},
                "agent_email": {"follows_main": True},
                "embedding": {"follows_main": True},
            },
            "confirm_warnings": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["main_model"] == "gemma4_e4b_ollama"
    assert tr.get_routing_for(AITask.STRUCTURED_EXTRACTION).primary == "gemma4_e4b_ollama"
    assert tr.get_routing_for(AITask.EMAIL_DRAFTING).primary == "gemma4_e4b_ollama"
    slots = {s["slot"]: s for s in data["slots"]}
    assert slots["structured_extraction"]["follows_main"] is True
    # Embeddings are another kind of model: they never follow.
    assert slots["embedding"]["can_follow_main"] is False
    assert slots["embedding"]["follows_main"] is False
    cfg = ac.get_builtin_agent_config()
    assert cfg.main_model == "gemma4_e4b_ollama"
    assert "embedding" not in cfg.main_model_slots

    # One change of the main model moves every follower in one revision.
    again = await client.post(
        "/api/providers/assignment-draft/apply",
        json={"main_model": "qwen3_5_9b_ollama", "confirm_warnings": True},
    )
    assert again.status_code == 200, again.text
    assert {d["slot"] for d in again.json()["diff"]} >= {"structured_extraction", "agent_email"}
    assert tr.get_routing_for(AITask.EMAIL_DRAFTING).primary == "qwen3_5_9b_ollama"


@pytest.mark.asyncio
async def test_a_slot_that_stops_following_keeps_its_own_model(
    client: AsyncClient, routing_mem_store, loaded
):
    await client.post(
        "/api/providers/assignment-draft/apply",
        json={
            "main_model": "gemma4_e4b_ollama",
            "slots": {"structured_extraction": {"follows_main": True}},
            "confirm_warnings": True,
        },
    )
    resp = await client.post(
        "/api/providers/assignment-draft/apply",
        json={
            "slots": {
                "structured_extraction": {"model": "qwen3_5_9b_ollama", "follows_main": False}
            },
            "confirm_warnings": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert tr.get_routing_for(AITask.STRUCTURED_EXTRACTION).primary == "qwen3_5_9b_ollama"
    slot = next(s for s in resp.json()["slots"] if s["slot"] == "structured_extraction")
    assert slot["follows_main"] is False


def test_a_switch_that_moves_the_followers_moves_the_main_model(routing_mem_store):
    ac.update_builtin_agent_config(
        ac.BuiltinAgentConfigUpdate(
            main_model="a", main_model_slots=["agent_orchestrator", "agent_email", "ocr_fast"]
        )
    )
    moved = pa._follow_moved_main(
        [
            pa.AssignmentDiffItem(
                slot="agent_orchestrator", old_model="a", new_model="b", affected=[]
            ),
            pa.AssignmentDiffItem(slot="agent_email", old_model="a", new_model="b", affected=[]),
        ]
    )
    assert moved is True
    cfg = ac.get_builtin_agent_config()
    assert cfg.main_model == "b"
    # ocr_fast stayed on «a»: it no longer follows, so it is not shown as «b».
    assert cfg.main_model_slots == ["agent_email", "agent_orchestrator"]
