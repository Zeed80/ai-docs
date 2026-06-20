"""Persistent runtime model config with Redis cache hydration.

The model registry still reads Redis overlays synchronously. This module makes
those overlays durable by mirroring runtime catalog entries and per-model
overrides into Postgres, then hydrating Redis before settings/API reads.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentConfigStore,
    ModelAssignmentRevision,
    ModelCatalogRuntimeEntry,
    ModelRuntimeOverride,
    TaskRoutingOverride,
)

logger = structlog.get_logger()

CATALOG_OVERLAY_KEY = "model_catalog_overlay"
THINKING_OVERLAY_KEY = "model_thinking_overrides"
PREFERRED_INSTANCE_KEY = "model_preferred_instances"
_AGENT_CONFIG_SINGLETON = "default"


def _redis_set_json(key: str, value: Any) -> None:
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().set(key, json.dumps(value, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_runtime_redis_write_failed", key=key, error=str(exc))


async def _upsert(db: AsyncSession, model, *, index_elements: list[str], values: dict[str, Any]) -> None:
    """Idempotent INSERT … ON CONFLICT DO UPDATE — race-safe under concurrency.

    Avoids the select-then-insert race that violated the UNIQUE constraint when
    two requests (e.g. parallel /live-models calls) wrote the same key at once.
    """
    update_cols = {k: v for k, v in values.items() if k not in index_elements}
    stmt = pg_insert(model).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=index_elements, set_=update_cols)
    await db.execute(stmt)


async def hydrate_runtime_cache(db: AsyncSession) -> None:
    """Rebuild Redis overlays from durable Postgres rows.

    Restores EVERYTHING settable in the UI — catalog, per-model overrides,
    task-routing assignments and the agent config — so a Redis flush/restart no
    longer silently reverts assignments to YAML defaults. Call on startup and
    AFTER each durable write's commit (never from inside an uncommitted txn).
    """
    catalog_rows = (
        await db.execute(select(ModelCatalogRuntimeEntry).order_by(ModelCatalogRuntimeEntry.model_key))
    ).scalars().all()
    override_rows = (
        await db.execute(select(ModelRuntimeOverride).order_by(ModelRuntimeOverride.model_key))
    ).scalars().all()
    routing_rows = (
        await db.execute(select(TaskRoutingOverride).order_by(TaskRoutingOverride.task))
    ).scalars().all()
    agent_cfg = await db.scalar(
        select(AgentConfigStore).where(AgentConfigStore.singleton_key == _AGENT_CONFIG_SINGLETON)
    )

    catalog = {row.model_key: row.capability for row in catalog_rows}
    thinking = {
        row.model_key: bool(row.thinking_enabled)
        for row in override_rows
        if row.thinking_enabled is not None
    }
    preferred = {
        row.model_key: row.preferred_instance
        for row in override_rows
        if row.preferred_instance
    }
    _redis_set_json(CATALOG_OVERLAY_KEY, catalog)
    _redis_set_json(THINKING_OVERLAY_KEY, thinking)
    _redis_set_json(PREFERRED_INSTANCE_KEY, preferred)

    # Assignments: restore the task_routing overlay and agent_config blob into
    # the same Redis keys the (sync) routing/config modules read.
    if routing_rows:
        from app.ai.task_routing import _REDIS_KEY as TASK_ROUTING_KEY

        _redis_set_json(TASK_ROUTING_KEY, {row.task: row.routing for row in routing_rows})
    if agent_cfg and agent_cfg.config:
        from app.ai.agent_config import _redis_set_agent_config

        try:
            _redis_set_agent_config(agent_cfg.config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_config_hydrate_failed", error=str(exc))


async def persist_catalog_entry(
    db: AsyncSession,
    *,
    model_key: str,
    provider: str,
    provider_model: str,
    capability: dict[str, Any],
    source: str = "discovered",
    verification_status: str = "discovered",
) -> None:
    """Durable catalog entry (race-safe upsert). Caller commits, then hydrates."""
    await _upsert(
        db,
        ModelCatalogRuntimeEntry,
        index_elements=["model_key"],
        values={
            "model_key": model_key,
            "provider": provider,
            "provider_model": provider_model,
            "capability": capability,
            "source": source,
            "verification_status": verification_status,
        },
    )


async def persist_model_override(
    db: AsyncSession,
    *,
    model_key: str,
    thinking_enabled: bool | None = None,
    preferred_instance: str | None = None,
    verification_status: str | None = None,
    notes: str | None = None,
) -> None:
    """Durable per-model override. Partial: only provided fields are written.

    Because partial updates must not clobber sibling columns, read-modify-write
    the row (still safe: single writer per model in practice; the UNIQUE key
    protects against duplicate inserts). Caller commits, then hydrates.
    """
    row = await db.scalar(
        select(ModelRuntimeOverride).where(ModelRuntimeOverride.model_key == model_key)
    )
    if row is None:
        row = ModelRuntimeOverride(model_key=model_key)
        db.add(row)
    if thinking_enabled is not None:
        row.thinking_enabled = bool(thinking_enabled)
    if preferred_instance is not None:
        row.preferred_instance = preferred_instance or None
    if verification_status is not None:
        row.verification_status = verification_status
    if notes is not None:
        row.notes = notes
    await db.flush()


async def persist_task_routing(db: AsyncSession, *, task: str, routing: dict[str, Any]) -> None:
    """Durable task-routing assignment (upsert). Caller commits, then hydrates."""
    await _upsert(
        db,
        TaskRoutingOverride,
        index_elements=["task"],
        values={"task": task, "routing": routing},
    )


async def persist_agent_config(db: AsyncSession, *, config: dict[str, Any]) -> None:
    """Durable agent-config blob (singleton upsert). Caller commits, then hydrates."""
    await _upsert(
        db,
        AgentConfigStore,
        index_elements=["singleton_key"],
        values={"singleton_key": _AGENT_CONFIG_SINGLETON, "config": config},
    )


async def create_assignment_revision(
    db: AsyncSession,
    *,
    created_by: str,
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any],
    diff: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> ModelAssignmentRevision:
    row = ModelAssignmentRevision(
        created_by=created_by or "system",
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        diff=diff,
        warnings=warnings,
    )
    db.add(row)
    await db.flush()
    return row


async def get_assignment_revision(
    db: AsyncSession,
    revision_id: str,
) -> ModelAssignmentRevision | None:
    return await db.get(ModelAssignmentRevision, revision_id)
