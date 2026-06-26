"""Durability + idempotency for the model runtime store (Fix 1/2/4).

Assignments and discovered catalog entries must survive a Redis flush (restored
from Postgres on hydrate), and concurrent persistence must not violate the
UNIQUE constraint.
"""

import asyncio
import json

import pytest

from app.ai import model_runtime_store as store
from app.db.models import ModelCatalogRuntimeEntry, TaskRoutingOverride


@pytest.fixture
def captured_redis(monkeypatch):
    """Capture what hydrate writes to Redis without a live server."""
    written: dict[str, object] = {}
    monkeypatch.setattr(
        store, "_redis_set_json", lambda k, v: written.__setitem__(k, v)
    )
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
    await store.persist_agent_config(db_session, config={"fast_model": "gemma4:e2b"})
    await db_session.commit()
    await store.hydrate_runtime_cache(db_session)
    assert written["cfg"]["fast_model"] == "gemma4:e2b"


@pytest.mark.asyncio
async def test_model_thinking_override_roundtrip_survives_flush(db_session, captured_redis):
    await store.persist_model_override(
        db_session,
        model_key="qwen3_5_9b_ollama",
        thinking_enabled=True,
    )
    await db_session.commit()
    await store.hydrate_runtime_cache(db_session)

    assert captured_redis[store.THINKING_OVERLAY_KEY]["qwen3_5_9b_ollama"] is True


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
        await db_session.execute(
            __import__("sqlalchemy").select(ModelCatalogRuntimeEntry).where(
                ModelCatalogRuntimeEntry.model_key == "k1"
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].provider_model == "gemma4:e2b-v2"  # last write wins
