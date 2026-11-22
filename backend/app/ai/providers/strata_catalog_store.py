"""Durable snapshots and hot cache for the official Strata model catalog."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.model_runtime_store import (
    hydrate_runtime_cache,
    persist_catalog_entry,
)
from app.db.models import StrataCatalogSnapshot

logger = structlog.get_logger()

CATALOG_CACHE_KEY = "strata:catalog:v1"
CATALOG_META_KEY = "strata:catalog:meta:v1"
CATALOG_LOCK_KEY = "strata:catalog:refresh-lock:v1"

_MEMORY_CATALOG: dict[str, Any] | None = None
_REFRESH_LOCK = asyncio.Lock()


class StrataCatalogRefreshInProgress(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _redis_get_json(key: str) -> Any | None:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _redis_set_json(key: str, value: Any) -> None:
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().set(key, json.dumps(value, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("strata_catalog_cache_write_failed", key=key, error=str(exc))


def _payload(catalog: Any) -> dict[str, Any]:
    return catalog.model_dump(mode="json", exclude_none=True)


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_catalog(catalog: Any) -> None:
    global _MEMORY_CATALOG
    payload = _payload(catalog)
    _MEMORY_CATALOG = payload
    _redis_set_json(CATALOG_CACHE_KEY, payload)


def current_catalog():
    """Return the last valid catalog; Redis is never the durable source of truth."""
    from app.ai.providers.strata_catalog import StrataCatalogV1, load_bundled_catalog

    payload = _redis_get_json(CATALOG_CACHE_KEY)
    if payload is None:
        payload = _MEMORY_CATALOG
    if payload is not None:
        try:
            return StrataCatalogV1.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("strata_catalog_cache_invalid", error=str(exc))
    return load_bundled_catalog()


def catalog_status() -> dict[str, Any]:
    catalog = current_catalog()
    meta = _redis_get_json(CATALOG_META_KEY) or {}
    return {
        "schema_version": catalog.schema_version,
        "source": catalog.source,
        "source_revision": catalog.source_revision,
        "feed_revision": catalog.feed_revision,
        "fetched_at": catalog.fetched_at,
        "last_attempt_at": meta.get("last_attempt_at"),
        "last_error": meta.get("last_error"),
        "warnings": meta.get("warnings") or [],
        "refreshing": _REFRESH_LOCK.locked(),
    }


def _record_meta(**updates: Any) -> None:
    meta = _redis_get_json(CATALOG_META_KEY) or {}
    meta.update(updates)
    _redis_set_json(CATALOG_META_KEY, meta)


async def _latest_snapshot(db: AsyncSession) -> StrataCatalogSnapshot | None:
    return await db.scalar(
        select(StrataCatalogSnapshot)
        .order_by(StrataCatalogSnapshot.fetched_at.desc(), StrataCatalogSnapshot.created_at.desc())
        .limit(1)
    )


async def _publish_families(db: AsyncSession, catalog: Any) -> None:
    from app.ai.providers.strata_catalog import catalog_capability

    for family in catalog.families:
        capability = catalog_capability(family)
        await persist_catalog_entry(
            db,
            model_key=family.catalog_key,
            provider="strata",
            provider_model=family.served_name,
            capability=capability.model_dump(mode="json", exclude={"name"}),
            source="strata_official",
            verification_status=capability.capability_source,
        )


def _new_snapshot(catalog: Any) -> StrataCatalogSnapshot:
    payload = _payload(catalog)
    return StrataCatalogSnapshot(
        schema_version=catalog.schema_version,
        source=catalog.source,
        source_revision=catalog.source_revision,
        feed_revision=catalog.feed_revision,
        payload=payload,
        sha256=_digest(payload),
        fetched_at=catalog.fetched_at,
    )


async def ensure_strata_catalog(db: AsyncSession):
    """Hydrate the hot cache, seeding the legacy catalog only on an empty store."""
    from app.ai.providers.strata_catalog import StrataCatalogV1, load_bundled_catalog

    snapshot = await _latest_snapshot(db)
    if snapshot is None:
        catalog = load_bundled_catalog()
        db.add(_new_snapshot(catalog))
        await _publish_families(db, catalog)
        await db.commit()
    else:
        catalog = StrataCatalogV1.model_validate(snapshot.payload)
    _cache_catalog(catalog)
    return catalog


async def store_refreshed_catalog(db: AsyncSession, incoming: Any):
    """Merge, persist and publish one validated source result atomically."""
    from app.ai.providers.strata_catalog import StrataCatalogV1, merge_catalogs

    snapshot = await _latest_snapshot(db)
    previous = (
        StrataCatalogV1.model_validate(snapshot.payload)
        if snapshot is not None
        else current_catalog()
    )
    merged, counts = merge_catalogs(previous, incoming)
    if snapshot is not None and _digest(_payload(merged)) == snapshot.sha256:
        await _publish_families(db, merged)
        await db.commit()
        _cache_catalog(merged)
        await hydrate_runtime_cache(db)
        return merged, {**counts, "changed": False}
    db.add(_new_snapshot(merged))
    await _publish_families(db, merged)
    await db.commit()
    _cache_catalog(merged)
    await hydrate_runtime_cache(db)
    return merged, {**counts, "changed": True}


def _acquire_distributed_lock(token: str) -> bool | None:
    """None means Redis is unavailable and the process-local lock is the only guard."""
    try:
        from app.utils.redis_client import get_sync_redis

        return bool(get_sync_redis().set(CATALOG_LOCK_KEY, token, nx=True, ex=600))
    except Exception as exc:  # noqa: BLE001
        logger.warning("strata_catalog_lock_unavailable", error=str(exc))
        return None


def _release_distributed_lock(token: str) -> None:
    try:
        from app.utils.redis_client import get_sync_redis

        client = get_sync_redis()
        current = client.get(CATALOG_LOCK_KEY)
        if isinstance(current, bytes):
            current = current.decode()
        if current == token:
            client.delete(CATALOG_LOCK_KEY)
    except Exception:
        pass


async def refresh_strata_catalog(
    db: AsyncSession,
    loader: Callable[[], Awaitable[Any]],
):
    """Run a source refresh without ever replacing last-known-good on failure."""
    if _REFRESH_LOCK.locked():
        raise StrataCatalogRefreshInProgress("Обновление каталога уже выполняется")
    async with _REFRESH_LOCK:
        token = f"{os.getpid()}:{uuid.uuid4()}"
        distributed = _acquire_distributed_lock(token)
        if distributed is False:
            raise StrataCatalogRefreshInProgress("Обновление каталога уже выполняется")
        try:
            _record_meta(last_attempt_at=_now().isoformat(), last_error=None)
            try:
                loaded = await loader()
                warnings = getattr(loaded, "warnings", [])
                incoming = getattr(loaded, "catalog", loaded)
                result = await store_refreshed_catalog(db, incoming)
            except Exception as exc:
                await db.rollback()
                _record_meta(last_error=str(exc)[:1000])
                raise
            _record_meta(last_error=None, last_success_at=_now().isoformat(), warnings=warnings)
            return result
        finally:
            if distributed is not None:
                _release_distributed_lock(token)
