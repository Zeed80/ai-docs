"""Workspace block store — Redis-backed with in-memory fallback.

Blocks survive backend restarts thanks to Redis persistence.
If Redis is unavailable the store degrades to the previous in-memory dict.
TTL defaults to 24 h so blocks are cleaned up automatically without manual
intervention while still surviving routine container restarts.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_REDIS_KEY = "workspace:blocks"
_BLOCK_TTL = 86_400  # 24 h in seconds

# In-memory fallback (used when Redis is unavailable)
_FALLBACK: dict[str, dict[str, Any]] = {}


def _redis():
    """Return sync Redis client or None on error."""
    try:
        from app.utils.redis_client import get_sync_redis
        return get_sync_redis()
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def upsert_workspace_block(block_id: str, block: dict[str, Any]) -> dict[str, Any]:
    now = _now_iso()
    r = _redis()
    existing: dict[str, Any] | None = None

    if r is not None:
        try:
            raw = r.hget(_REDIS_KEY, block_id)
            if raw:
                existing = json.loads(raw)
        except Exception:
            pass

    if existing is None:
        existing = _FALLBACK.get(block_id)

    stored = {
        **block,
        "id": block_id,
        "created_at": existing.get("created_at") if existing else now,
        "updated_at": now,
    }

    if r is not None:
        try:
            r.hset(_REDIS_KEY, block_id, json.dumps(stored, default=str))
            r.expire(_REDIS_KEY, _BLOCK_TTL)
        except Exception as exc:
            logger.warning("workspace_redis_write_failed", extra={"error": str(exc)})
            _FALLBACK[block_id] = stored
    else:
        _FALLBACK[block_id] = stored

    return stored


def append_workspace_block(block: dict[str, Any]) -> dict[str, Any]:
    block_id = str(block.get("id") or f"workspace:{_count() + 1}")
    return upsert_workspace_block(block_id, block)


def _count() -> int:
    r = _redis()
    if r is not None:
        try:
            return int(r.hlen(_REDIS_KEY))
        except Exception:
            pass
    return len(_FALLBACK)


def list_workspace_blocks() -> list[dict[str, Any]]:
    r = _redis()
    items: list[dict[str, Any]] = []

    if r is not None:
        try:
            raw_map = r.hgetall(_REDIS_KEY)
            for v in raw_map.values():
                try:
                    items.append(json.loads(v))
                except Exception:
                    pass
        except Exception:
            items = list(_FALLBACK.values())
    else:
        items = list(_FALLBACK.values())

    return sorted(items, key=lambda b: str(b.get("updated_at", "")), reverse=True)


def get_workspace_block(block_id: str) -> dict[str, Any] | None:
    r = _redis()
    if r is not None:
        try:
            raw = r.hget(_REDIS_KEY, block_id)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    return _FALLBACK.get(block_id)


def delete_workspace_block(block_id: str) -> bool:
    deleted = False
    r = _redis()
    if r is not None:
        try:
            deleted = bool(r.hdel(_REDIS_KEY, block_id))
        except Exception:
            pass
    if block_id in _FALLBACK:
        _FALLBACK.pop(block_id)
        deleted = True
    return deleted


def clear_workspace_blocks() -> None:
    r = _redis()
    if r is not None:
        try:
            r.delete(_REDIS_KEY)
        except Exception:
            pass
    _FALLBACK.clear()
