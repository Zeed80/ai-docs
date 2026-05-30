"""Unit tests for the simplified two-group assignment layer."""

import pytest

from app.ai import agent_config as ac
from app.ai import assignment_groups as ag
from app.ai import task_routing as tr
from app.ai.schemas import AITask


@pytest.fixture
def stores(monkeypatch, tmp_path):
    """In-memory replacements for the Redis-backed task_routing + agent_config."""
    routing_store: dict[str, dict] = {}
    monkeypatch.setattr(tr, "_redis_get", lambda: dict(routing_store) if routing_store else None)

    def _routing_set(value):
        routing_store.clear()
        routing_store.update(value)

    monkeypatch.setattr(tr, "_redis_set", _routing_set)

    agent_store: dict = {}
    monkeypatch.setattr(ac, "_redis_get_agent_config", lambda: dict(agent_store) if agent_store else None)
    monkeypatch.setattr(ac, "_redis_set_agent_config", lambda data: agent_store.update(data))
    monkeypatch.setattr(ac, "_CONFIG_FILE", tmp_path / "agent_config.json")

    # Avoid touching the legacy ai_config file/redis from the mirror.
    import app.api.ai_settings as ai_settings
    cfg: dict = {}
    monkeypatch.setattr(ai_settings, "get_ai_config", lambda: dict(cfg))
    monkeypatch.setattr(ai_settings, "save_ai_config", lambda c: cfg.update(c))

    return {"routing": routing_store, "agent": agent_store, "ai_config": cfg}


def test_set_document_group_fans_out_to_tasks(stores):
    ag.set_document_group(
        ag.DocumentGroup(
            vision_model="gemma4_e4b_ollama",
            text_model="qwen3_6_35b_ollama",
            embedding_model="qwen3_embedding_8b_ollama",
            rerank_model="local_reranker_ollama",
        )
    )

    # Vision slot drives OCR + drawings.
    for task in ag.VISION_DOC_TASKS:
        assert tr.get_routing_for(task).models[0] == "gemma4_e4b_ollama"
    # Text slot drives reasoning / email / etc.
    for task in ag.TEXT_DOC_TASKS:
        assert tr.get_routing_for(task).models[0] == "qwen3_6_35b_ollama"

    assert tr.get_routing_for(AITask.EMBEDDING).models[0] == "qwen3_embedding_8b_ollama"
    assert tr.get_routing_for(AITask.RERANKING).models[0] == "local_reranker_ollama"


def test_set_document_group_keeps_confidential_local(stores):
    ag.set_document_group(ag.DocumentGroup(vision_model="gemma4_e4b_ollama"))
    ocr = tr.get_routing_for(AITask.INVOICE_OCR)
    assert ocr.local_only is True and ocr.allow_cloud is False


def test_set_document_group_mirrors_ai_config(stores):
    ag.set_document_group(
        ag.DocumentGroup(
            vision_model="gemma4_e4b_ollama",
            embedding_model="qwen3_embedding_8b_ollama",
        )
    )
    cfg = stores["ai_config"]
    # vision → raw provider name for the legacy OCR/VLM fields.
    assert cfg["model_ocr"] == "gemma4:e4b"
    assert cfg["model_vlm"] == "gemma4:e4b"
    # embedding → catalog key as-is.
    assert cfg["embedding_model"] == "qwen3_embedding_8b_ollama"


def test_set_document_group_skips_none_slots(stores):
    before = tr.get_routing_for(AITask.EMBEDDING).models[0]
    ag.set_document_group(ag.DocumentGroup(vision_model="gemma4_e4b_ollama"))
    # Embedding untouched when its slot is None.
    assert tr.get_routing_for(AITask.EMBEDDING).models[0] == before


def test_set_agent_group_updates_roles_and_syncs_orchestrator(stores):
    ag.set_agent_group(
        ag.AgentGroup(
            agent_model="gemma4:e4b",
            agent_provider="ollama",
            large_model="qwen3.6:35b",
            large_provider="ollama",
        )
    )
    cfg = ac.get_builtin_agent_config()
    assert cfg.worker_model == "gemma4:e4b"
    assert cfg.orchestrator_model == "gemma4:e4b"
    assert cfg.auditor_model == "gemma4:e4b"
    assert cfg.builder_model == "qwen3.6:35b"

    # Critical: orchestrator routing must follow so the pinned model is correct.
    orch = tr.get_routing_for(AITask.ORCHESTRATOR_PLANNING)
    assert orch.models[0] == "gemma4_e4b_ollama"
    tool = tr.get_routing_for(AITask.TOOL_CALLING)
    assert tool.models[0] == "gemma4_e4b_ollama"


def test_get_groups_roundtrip(stores):
    ag.set_document_group(ag.DocumentGroup(text_model="qwen3_6_35b_ollama"))
    ag.set_agent_group(ag.AgentGroup(agent_model="gemma4:e4b", agent_provider="ollama"))
    groups = ag.get_groups()
    assert groups["documents"]["text_model"] == "qwen3_6_35b_ollama"
    assert groups["agent"]["agent_model"] == "gemma4:e4b"
