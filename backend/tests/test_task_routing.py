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


def test_confidential_task_rejects_cloud_model(mem_store):
    # A confidential task must not accept a cloud model in its chain.
    keys = list(tr.known_model_keys())
    cloud_key = next(k for k in keys if "anthropic" in k or "google" in k)
    with pytest.raises(ValueError, match="non-local"):
        tr.save_task_routing(
            AITask.INVOICE_OCR,
            tr.TaskRouting(
                task="invoice_ocr",
                models=[cloud_key],
                profile="anti_hallucination",
                local_only=False,
                allow_cloud=True,
            ),
        )
    # Default stays local-only.
    assert tr.get_routing_for(AITask.INVOICE_OCR).local_only is True


def test_confidential_task_accepts_local_and_locks_policy(mem_store):
    # Local models are fine; policy is forced local even if cloud was requested.
    local_key = next(k for k in tr.known_model_keys() if k.endswith("_ollama"))
    saved = tr.save_task_routing(
        AITask.INVOICE_OCR,
        tr.TaskRouting(
            task="invoice_ocr",
            models=[local_key],
            profile="anti_hallucination",
            local_only=False,
            allow_cloud=True,
        ),
    )
    assert saved.local_only is True
    assert saved.allow_cloud is False


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
            "model_ocr": "gemma4:e4b",
            "model_ocr_provider": "ollama",
            "model_reasoning": "claude-sonnet-4-6",
            "model_reasoning_provider": "anthropic",
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


def test_migration_covers_embedding_and_reranking(mem_store, monkeypatch):
    """embedding_model / reranker_model hold catalog keys (no _provider sibling)."""
    import app.api.ai_settings as ai_settings

    monkeypatch.setattr(
        ai_settings,
        "get_ai_config",
        lambda: {
            "embedding_model": "qwen3_embedding_8b_ollama",
            "reranker_model": "local_reranker_ollama",
        },
    )
    result = tr.migrate_from_ai_config()
    assert result["migrated"] is True

    emb = tr.get_routing_for(AITask.EMBEDDING)
    assert emb.models[0] == "qwen3_embedding_8b_ollama"
    assert emb.local_only is True

    rer = tr.get_routing_for(AITask.RERANKING)
    assert rer.models[0] == "local_reranker_ollama"
    assert rer.local_only is True


@pytest.mark.asyncio
async def test_routing_change_survives_a_restart(mem_store, db_session, monkeypatch):
    """A slot assigned through /routing/{task} must still be that model after a
    restart.

    Startup rebuilds the Redis routing key from Postgres
    (``model_runtime_store.hydrate_runtime_cache``) and ``save_task_routing``
    writes to Redis ONLY — so before this was fixed, choosing a model here and
    restarting the backend silently brought the old model back, with nothing in
    the logs to say so (measured on the live stand).
    """
    from app.ai import model_runtime_store

    task = AITask.STRUCTURED_EXTRACTION
    base = tr.get_routing_for(task)
    # Deliberately NOT the YAML default for this task — otherwise the
    # assertion would hold even with nothing restored at all.
    chosen = "gemma4_e4b_ollama"
    assert base.models[0] != chosen
    tr.save_task_routing(
        task,
        base.model_copy(update={"models": [chosen, *[m for m in base.models if m != chosen]]}),
    )
    await model_runtime_store.persist_routing_snapshot(db_session, [task.value])
    await db_session.commit()

    # Hydration writes the Redis keys directly; route the routing key into the
    # same in-memory store the routing module reads.
    def _fake_set_json(key, value):
        if key == tr._REDIS_KEY:
            mem_store.clear()
            mem_store.update(value)

    monkeypatch.setattr(model_runtime_store, "_redis_set_json", _fake_set_json)

    # The restart: the Redis overlay is gone, startup restores it from Postgres.
    mem_store.clear()
    await model_runtime_store.hydrate_runtime_cache(db_session)

    assert mem_store, "перезапуск не восстановил назначения из Postgres"
    assert tr.get_routing_for(task).models[0] == chosen
