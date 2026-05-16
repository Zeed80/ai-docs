"""Shared Redis connection pool singletons.

Usage:
  Sync contexts (Celery tasks, background threads, sync helpers):
    from app.utils.redis_client import get_sync_redis
    r = get_sync_redis()
    r.get("key")

  Async contexts (FastAPI endpoints, middleware):
    from app.utils.redis_client import get_async_redis
    r = get_async_redis()
    await r.ping()

  Lifespan shutdown:
    from app.utils.redis_client import close_pools
    await close_pools()
"""
from __future__ import annotations

import redis
import redis.asyncio as aioredis
from redis.connection import ConnectionPool
from redis.asyncio.connection import ConnectionPool as AsyncConnectionPool

_sync_pool: ConnectionPool | None = None
_async_pool: AsyncConnectionPool | None = None


def get_sync_redis() -> redis.Redis:
    """Return a Redis client backed by a shared sync connection pool."""
    global _sync_pool
    if _sync_pool is None:
        from app.config import settings
        _sync_pool = ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=10,
        )
    return redis.Redis(connection_pool=_sync_pool)


def get_async_redis() -> aioredis.Redis:
    """Return an async Redis client backed by a shared async connection pool."""
    global _async_pool
    if _async_pool is None:
        from app.config import settings
        _async_pool = AsyncConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=20,
        )
    return aioredis.Redis(connection_pool=_async_pool)


async def close_pools() -> None:
    """Gracefully close both pools. Call from FastAPI lifespan shutdown."""
    global _sync_pool, _async_pool
    if _async_pool is not None:
        await _async_pool.aclose()
        _async_pool = None
    if _sync_pool is not None:
        _sync_pool.disconnect()
        _sync_pool = None
