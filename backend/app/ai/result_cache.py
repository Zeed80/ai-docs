"""Tiny short-TTL cache for frequent read aggregates (counts, dashboards).

Speeds up repeated deterministic questions ("сколько счетов") on weak local
models by skipping the backend round-trip when the same answer was produced a
few seconds ago. Redis-backed, best-effort: any failure degrades to a miss, so
correctness never depends on the cache.

Entries belong to the acting user (``cache_scope``): a count is computed under
that user's row rights, and serving it to another user leaked how many
documents, invoices or mails they could not see. No acting user — no cache.
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


def _scoped(key: str) -> str | None:
    from app.ai.actor_context import cache_scope

    scope = cache_scope()
    if not key or scope is None:
        return None
    return f"{_PREFIX}{scope}:{key}"


def cache_get(key: str) -> str | None:
    """Return the cached string for *key*, or None on miss / no Redis."""
    full_key = _scoped(key)
    if full_key is None:
        return None
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(full_key)
        if raw is None:
            return None
        return raw if isinstance(raw, str) else raw.decode("utf-8")
    except Exception:
        return None


def cache_set(key: str, value: str, ttl: int = _DEFAULT_TTL) -> None:
    """Best-effort store of *value* under *key* with a short TTL."""
    full_key = _scoped(key)
    if full_key is None or value is None:
        return
    r = _redis()
    if r is None:
        return
    try:
        r.setex(full_key, ttl, value)
    except Exception:
        pass
