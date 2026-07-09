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

import asyncio

import redis
import redis.asyncio as aioredis
from redis.connection import ConnectionPool
from redis.asyncio.connection import BlockingConnectionPool as AsyncBlockingConnectionPool
from redis.asyncio.connection import ConnectionPool as AsyncConnectionPool

_sync_pool: ConnectionPool | None = None
_async_pool: AsyncConnectionPool | None = None
_async_pool_loop: asyncio.AbstractEventLoop | None = None
_async_pubsub_pool: AsyncConnectionPool | None = None
_async_pubsub_pool_loop: asyncio.AbstractEventLoop | None = None


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


def _dispose_pool_on_its_loop(
    pool: AsyncConnectionPool, loop: asyncio.AbstractEventLoop | None
) -> None:
    """Best-effort close of a pool bound to another (possibly dead) event loop."""
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(pool.aclose()))
    except RuntimeError:
        pass  # loop closed between the check and the call


def get_async_redis() -> aioredis.Redis:
    """Return an async Redis client backed by a pool bound to the current loop."""
    global _async_pool, _async_pool_loop
    current_loop = asyncio.get_running_loop()
    if _async_pool is None or _async_pool_loop is not current_loop:
        if _async_pool is not None:
            _dispose_pool_on_its_loop(_async_pool, _async_pool_loop)
        from app.config import settings
        _async_pool = AsyncBlockingConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=100,
            timeout=5,
        )
        _async_pool_loop = current_loop
    return aioredis.Redis(connection_pool=_async_pool)


def get_async_redis_pubsub() -> aioredis.Redis:
    """Return an async Redis client for long-lived pub/sub subscriptions.

    Backed by its own pool, separate from ``get_async_redis()``'s shared
    command pool. A pub/sub connection is held for the entire lifetime of a
    subscription (e.g. one per open Studio queue SSE tab) and only returns to
    its pool when the subscriber disconnects — sharing that pool with normal
    short-lived request-path calls (auth revocation checks, rate limiting)
    let enough open tabs starve unrelated requests of connections.
    """
    global _async_pubsub_pool, _async_pubsub_pool_loop
    current_loop = asyncio.get_running_loop()
    if _async_pubsub_pool is None or _async_pubsub_pool_loop is not current_loop:
        if _async_pubsub_pool is not None:
            _dispose_pool_on_its_loop(_async_pubsub_pool, _async_pubsub_pool_loop)
        from app.config import settings
        _async_pubsub_pool = AsyncConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=200,
        )
        _async_pubsub_pool_loop = current_loop
    return aioredis.Redis(connection_pool=_async_pubsub_pool)


async def close_pools() -> None:
    """Gracefully close both pools. Call from FastAPI lifespan shutdown."""
    global _sync_pool, _async_pool, _async_pool_loop, _async_pubsub_pool, _async_pubsub_pool_loop
    if _async_pool is not None:
        await _async_pool.aclose()
        _async_pool = None
        _async_pool_loop = None
    if _async_pubsub_pool is not None:
        await _async_pubsub_pool.aclose()
        _async_pubsub_pool = None
        _async_pubsub_pool_loop = None
    if _sync_pool is not None:
        _sync_pool.disconnect()
        _sync_pool = None
