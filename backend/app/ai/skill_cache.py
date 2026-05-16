"""Skill result cache — Redis-backed memoization for deterministic skill calls.

Caches the output of read-only skill calls so repeated identical queries
don't trigger LLM inference or expensive DB aggregations.

Cache key = SHA256(skill_name + canonical JSON of args)
TTL varies by skill category (invoices: 5 min, reports: 60 min, catalogs: 24 h).

Write-path skills (create/update/delete/approve) are NEVER cached.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import structlog

logger = structlog.get_logger()

# ── TTL policy (seconds) ───────────────────────────────────────────────────────

_DEFAULT_TTL = 300  # 5 min

_CATEGORY_TTL: dict[str, int] = {
    # Fast-changing: invoices, approvals, anomalies
    "invoices":     300,
    "approvals":    120,
    "anomalies":    180,
    # Medium: suppliers, warehouse
    "suppliers":    1_800,
    "warehouse":    900,
    "documents":    600,
    # Slow: catalogs, normalization, static tables
    "catalogs":     86_400,   # 24 h
    "normalization": 3_600,   # 1 h
    "reports":      3_600,
    "tables":       600,
    "workspace":    300,
}

# Prefixes that indicate write operations — never cache
_WRITE_PREFIXES = frozenset({
    "create", "update", "delete", "approve", "reject", "send",
    "submit", "post", "patch", "put", "remove", "archive",
    "export", "apply", "set", "assign",
})


def _is_cacheable(skill_name: str) -> bool:
    """Return True if this skill should be cached (read-only heuristic)."""
    lower = skill_name.lower().replace(".", "_")
    parts = lower.split("_")
    return parts[0] not in _WRITE_PREFIXES


def _ttl_for(skill_name: str) -> int:
    """Return TTL in seconds for the given skill name."""
    lower = skill_name.lower()
    for category, ttl in _CATEGORY_TTL.items():
        if category in lower:
            return ttl
    return _DEFAULT_TTL


def _cache_key(skill_name: str, args: dict[str, Any]) -> str:
    """Stable cache key from skill name and canonical args."""
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    payload = f"{skill_name}:{canonical}"
    return "skill_cache:" + hashlib.sha256(payload.encode()).hexdigest()[:32]


# ── Public API ─────────────────────────────────────────────────────────────────

async def get_cached(skill_name: str, args: dict[str, Any]) -> dict | None:
    """Return cached result or None. Non-blocking — errors are swallowed."""
    if not _is_cacheable(skill_name):
        return None
    try:
        from app.utils.redis_client import get_async_redis
        key = _cache_key(skill_name, args)
        raw = await get_async_redis().get(key)
        if raw:
            result = json.loads(raw)
            logger.debug("skill_cache_hit", skill=skill_name, key=key[:16])
            return result
    except Exception as exc:
        logger.debug("skill_cache_get_error", skill=skill_name, error=str(exc))
    return None


async def set_cached(
    skill_name: str,
    args: dict[str, Any],
    result: dict,
    *,
    ttl: int | None = None,
) -> None:
    """Store result in cache. Errors are swallowed."""
    if not _is_cacheable(skill_name):
        return
    # Don't cache error results
    if isinstance(result, dict) and result.get("status") in ("error", "stub"):
        return
    try:
        from app.utils.redis_client import get_async_redis
        key = _cache_key(skill_name, args)
        effective_ttl = ttl if ttl is not None else _ttl_for(skill_name)
        payload = json.dumps(result, ensure_ascii=False, default=str)
        await get_async_redis().setex(key, effective_ttl, payload)
        logger.debug("skill_cache_set", skill=skill_name, ttl=effective_ttl)
    except Exception as exc:
        logger.debug("skill_cache_set_error", skill=skill_name, error=str(exc))


async def invalidate_skill(skill_name: str) -> None:
    """Invalidate all cache entries for a skill (e.g. after data write)."""
    # Pattern-based deletion not efficient at scale; use Redis SCAN in production.
    # For now just log — full invalidation requires SCAN + DEL.
    logger.info("skill_cache_invalidate_requested", skill=skill_name)


async def get_cache_stats() -> dict[str, Any]:
    """Return basic cache statistics for monitoring."""
    try:
        from app.utils.redis_client import get_async_redis
        r = get_async_redis()
        info = await r.info("stats")
        keys_count = len(await r.keys("skill_cache:*"))
        return {
            "cached_skills": keys_count,
            "keyspace_hits": info.get("keyspace_hits", 0),
            "keyspace_misses": info.get("keyspace_misses", 0),
            "hit_rate": (
                info.get("keyspace_hits", 0)
                / max(1, info.get("keyspace_hits", 0) + info.get("keyspace_misses", 0))
            ),
        }
    except Exception:
        return {"cached_skills": 0, "hit_rate": 0.0}
