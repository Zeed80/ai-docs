"""Tiny short-TTL cache for frequent read aggregates (counts, dashboards).

Speeds up repeated deterministic questions ("сколько счетов") on weak local
models by skipping the backend round-trip when the same answer was produced a
few seconds ago. Redis-backed, best-effort: any failure degrades to a miss, so
correctness never depends on the cache.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

_PREFIX = "agent:result_cache:"
_DEFAULT_TTL = 15  # seconds — counts drift slowly; a few seconds of staleness is fine


def _redis():
    try:
        from app.utils.redis_client import get_sync_redis
        return get_sync_redis()
    except Exception:
        return None


def cache_get(key: str) -> str | None:
    """Return the cached string for *key*, or None on miss / no Redis."""
    if not key:
        return None
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(_PREFIX + key)
        if raw is None:
            return None
        return raw if isinstance(raw, str) else raw.decode("utf-8")
    except Exception:
        return None


def cache_set(key: str, value: str, ttl: int = _DEFAULT_TTL) -> None:
    """Best-effort store of *value* under *key* with a short TTL."""
    if not key or value is None:
        return
    r = _redis()
    if r is None:
        return
    try:
        r.setex(_PREFIX + key, ttl, value)
    except Exception:
        pass
