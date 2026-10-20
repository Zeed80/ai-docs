"""Durability + idempotency for the model runtime store (Fix 1/2/4).

Assignments and discovered catalog entries must survive a Redis flush (restored
from Postgres on hydrate), and concurrent persistence must not violate the
UNIQUE constraint.
"""

import pytest

from app.ai import model_runtime_store as store
from app.db.models import ModelCatalogRuntimeEntry


@pytest.fixture
def captured_redis(monkeypatch):
    """Capture what hydrate writes to Redis without a live server."""
    written: dict[str, object] = {}
    monkeypatch.setattr(store, "_redis_set_json", lambda k, v: written.__setitem__(k, v))
    return written


@pytest.mark.asyncio
async def test_task_routing_roundtrip_survives_flush(db_session, captured_redis):
    # Persist a task-routing override, commit, then hydrate from Postgres only.
    await store.persist_task_routing(
        db_session, task="embedding", routing={"task": "embedding", "models": ["m1"]}
    )
    await db_session.commit()
    await store.hydrate_runtime_cache(db_session)

    from app.ai.task_routing import _REDIS_KEY as TASK_KEY

    assert TASK_KEY in captured_redis
    assert captured_redis[TASK_KEY]["embedding"]["models"] == ["m1"]


@pytest.mark.asyncio
async def test_agent_config_roundtrip_survives_flush(db_session, monkeypatch):
    written = {}
    monkeypatch.setattr(
        "app.ai.agent_config._redis_set_agent_config",
        lambda data: written.update({"cfg": data}),
    )
    await store.persist_agent_config(db_session, config={"compression_model": "gemma4:e2b"})
    await db_session.commit()
    await store.hydrate_runtime_cache(db_session)
    assert written["cfg"]["compression_model"] == "gemma4:e2b"


@pytest.mark.asyncio
async def test_model_thinking_override_roundtrip_survives_flush(db_session, captured_redis):
    await store.persist_model_override(
        db_session,
        model_key="qwen3_5_9b_ollama",
        thinking_enabled=True,
        thinking_level="high",
    )
    await db_session.commit()
    await store.hydrate_runtime_cache(db_session)

    # {enabled, level, levels} shape — model_registry._load_thinking_overrides()
    # reads this and both legacy shapes defensively. thinking_levels was not
    # provided in this write, so it stays unset (None).
    overlay = captured_redis[store.THINKING_OVERLAY_KEY]["qwen3_5_9b_ollama"]
    assert overlay == {"enabled": True, "level": "high", "levels": None}


@pytest.mark.asyncio
async def test_model_thinking_levels_only_override_survives_flush(db_session, captured_redis):
    """The discovery loop's automatic level probe writes ONLY thinking_levels
    (never touching a human's enabled/level choice) — the row must still
    hydrate even though thinking_enabled stays NULL forever.
    """
    await store.persist_model_override(
        db_session,
        model_key="qwen3_6_35b_apex_ollama",
        thinking_levels=[],  # a real negative verdict, not "nothing to write"
    )
    await db_session.commit()
    await store.hydrate_runtime_cache(db_session)

    overlay = captured_redis[store.THINKING_OVERLAY_KEY]["qwen3_6_35b_apex_ollama"]
    assert overlay == {"enabled": None, "level": None, "levels": []}


@pytest.mark.asyncio
async def test_catalog_upsert_is_idempotent(db_session, captured_redis):
    # Two writes of the same model_key must not raise (ON CONFLICT DO UPDATE).
    for vmodel in ("gemma4:e2b", "gemma4:e2b-v2"):
        await store.persist_catalog_entry(
            db_session,
            model_key="k1",
            provider="ollama",
            provider_model=vmodel,
            capability={"x": 1},
        )
    await db_session.commit()
    rows = (
        (
            await db_session.execute(
                __import__("sqlalchemy")
                .select(ModelCatalogRuntimeEntry)
                .where(ModelCatalogRuntimeEntry.model_key == "k1")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].provider_model == "gemma4:e2b-v2"  # last write wins


@pytest.mark.asyncio
async def test_agent_settings_screen_survives_a_restart(db_session, monkeypatch, tmp_path):
    """«Настройки → Агент» saved Redis and the file only; the restart restored
    the old Postgres copy (max_steps 40 → 10, 2026-10-16)."""
    from app.ai import agent_config
    from app.api.ai_settings import patch_agent_config

    live: dict = {}
    monkeypatch.setattr(agent_config, "_redis_set_agent_config", lambda data: live.update(data))
    monkeypatch.setattr(agent_config, "_redis_get_agent_config", lambda: dict(live) or None)
    monkeypatch.setattr(agent_config, "_CONFIG_FILE", tmp_path / "agent_config.json")

    await patch_agent_config(agent_config.BuiltinAgentConfigUpdate(max_steps=40), db=db_session)

    restored: dict = {}
    monkeypatch.setattr(agent_config, "_redis_set_agent_config", lambda data: restored.update(data))
    await store.hydrate_runtime_cache(db_session)
    assert restored["max_steps"] == 40
