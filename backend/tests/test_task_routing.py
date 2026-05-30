"""Unit tests for the unified task_routing store (single source of truth)."""

import pytest

from app.ai import task_routing as tr
from app.ai.schemas import AITask


@pytest.fixture
def mem_store(monkeypatch):
    """In-memory replacement for the Redis-backed overlay."""
    store: dict[str, dict] = {}
    monkeypatch.setattr(tr, "_redis_get", lambda: dict(store) if store else None)

    def _set(value):
        store.clear()
        store.update(value)

    monkeypatch.setattr(tr, "_redis_set", _set)
    return store


def test_defaults_from_yaml(mem_store):
    routing = tr.get_task_routing()
    assert len(routing) == len(list(AITask))

    ocr = routing[AITask.INVOICE_OCR]
    assert ocr.models[0] == "gemma4_e4b_ollama"
    assert ocr.profile == "anti_hallucination"
    assert ocr.local_only is True
    assert ocr.allow_cloud is False

    # code_generation route is local_only: false in the YAML
    cg = routing[AITask.CODE_GENERATION]
    assert cg.local_only is False
    assert cg.allow_cloud is True


def test_confidential_task_cannot_go_cloud(mem_store):
    # Try to force OCR to cloud — must be re-locked to local.
    keys = list(tr.known_model_keys())
    cloud_key = next(k for k in keys if "anthropic" in k or "google" in k)
    saved = tr.save_task_routing(
        AITask.INVOICE_OCR,
        tr.TaskRouting(task="invoice_ocr", models=[cloud_key], profile="anti_hallucination",
                       local_only=False, allow_cloud=True),
    )
    assert saved.local_only is True
    assert saved.allow_cloud is False
    assert tr.get_routing_for(AITask.INVOICE_OCR).local_only is True


def test_save_and_reset_non_confidential(mem_store):
    cg_default = tr.get_routing_for(AITask.CODE_GENERATION)
    new = cg_default.model_copy(update={"profile": "balanced"})
    tr.save_task_routing(AITask.CODE_GENERATION, new)
    assert tr.get_routing_for(AITask.CODE_GENERATION).profile == "balanced"

    reverted = tr.reset_task_routing(AITask.CODE_GENERATION)
    assert reverted.profile == cg_default.profile


def test_validation_unknown_model(mem_store):
    with pytest.raises(ValueError, match="Unknown model"):
        tr.save_task_routing(
            AITask.ENGINEERING_REASONING,
            tr.TaskRouting(task="engineering_reasoning", models=["does_not_exist"]),
        )


def test_validation_unknown_profile(mem_store):
    keys = list(tr.known_model_keys())
    with pytest.raises(ValueError, match="Unknown inference profile"):
        tr.save_task_routing(
            AITask.ENGINEERING_REASONING,
            tr.TaskRouting(task="engineering_reasoning", models=[keys[0]], profile="nope"),
        )


def test_resolve_model_returns_provider_model(mem_store):
    model, provider = tr.resolve_model(AITask.INVOICE_OCR)
    assert model == "gemma4:e4b"
    assert provider == "ollama"


def test_migration_from_ai_config(mem_store, monkeypatch):
    import app.api.ai_settings as ai_settings

    monkeypatch.setattr(
        ai_settings,
        "get_ai_config",
        lambda: {
            "model_ocr": "gemma4:e4b", "model_ocr_provider": "ollama",
            "model_reasoning": "claude-sonnet-4-6", "model_reasoning_provider": "anthropic",
        },
    )
    result = tr.migrate_from_ai_config()
    assert result["migrated"] is True

    # Confidential OCR stays local.
    ocr = tr.get_routing_for(AITask.INVOICE_OCR)
    assert ocr.local_only is True and ocr.models[0] == "gemma4_e4b_ollama"

    # Cloud reasoning model migrated with cloud allowed.
    reasoning = tr.get_routing_for(AITask.ENGINEERING_REASONING)
    assert reasoning.models[0] == "claude_sonnet_anthropic"
    assert reasoning.local_only is False and reasoning.allow_cloud is True

    # Idempotent: second run is a no-op.
    assert tr.migrate_from_ai_config()["migrated"] is False
