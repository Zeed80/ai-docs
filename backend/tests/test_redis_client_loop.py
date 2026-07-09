from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from app.utils import redis_client


def test_async_redis_pool_is_recreated_for_a_new_event_loop() -> None:
    pools = [MagicMock(name="pool_one"), MagicMock(name="pool_two")]

    async def get_client_pool() -> object:
        return redis_client.get_async_redis().connection_pool

    redis_client._async_pool = None
    redis_client._async_pool_loop = None
    with (
        patch.object(
            redis_client.AsyncConnectionPool,
            "from_url",
            side_effect=pools,
        ) as from_url,
        patch.object(
            redis_client.aioredis,
            "Redis",
            side_effect=lambda *, connection_pool: MagicMock(
                connection_pool=connection_pool,
            ),
        ),
    ):
        first = asyncio.run(get_client_pool())
        second = asyncio.run(get_client_pool())

    assert first is pools[0]
    assert second is pools[1]
    assert from_url.call_count == 2
    redis_client._async_pool = None
    redis_client._async_pool_loop = None


def test_pubsub_pool_is_isolated_from_the_command_pool() -> None:
    """Streaming (SSE) subscribers must draw connections from a pool
    dedicated to pub/sub, not from the shared command pool used by
    short-lived request-path calls (auth checks, rate limiting)."""
    command_pool = MagicMock(name="command_pool")
    pubsub_pool = MagicMock(name="pubsub_pool")

    async def get_both_pools() -> tuple[object, object]:
        return (
            redis_client.get_async_redis().connection_pool,
            redis_client.get_async_redis_pubsub().connection_pool,
        )

    redis_client._async_pool = None
    redis_client._async_pool_loop = None
    redis_client._async_pubsub_pool = None
    redis_client._async_pubsub_pool_loop = None
    with (
        patch.object(
            redis_client.AsyncConnectionPool,
            "from_url",
            side_effect=[command_pool, pubsub_pool],
        ),
        patch.object(
            redis_client.aioredis,
            "Redis",
            side_effect=lambda *, connection_pool: MagicMock(
                connection_pool=connection_pool,
            ),
        ),
    ):
        command, pubsub = asyncio.run(get_both_pools())

    assert command is command_pool
    assert pubsub is pubsub_pool
    assert command is not pubsub
    redis_client._async_pool = None
    redis_client._async_pool_loop = None
    redis_client._async_pubsub_pool = None
    redis_client._async_pubsub_pool_loop = None
