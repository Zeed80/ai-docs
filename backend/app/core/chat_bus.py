"""Pub/sub bus for broadcasting chat events to WebSocket clients.

Architecture:
- Local callbacks are stored in-memory (per-worker).
- Events are published to Redis channels so all workers receive them.
- A background subscriber task dispatches Redis messages to local callbacks.

Channel naming (prefix defaults to "sveta:bus"):
  {prefix}:global         — broadcast to every subscriber
  {prefix}:user:{sub}     — push to a specific user
  {prefix}:room:{room_id} — push to room members
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Awaitable

import structlog

logger = structlog.get_logger()

ChatCallback = Callable[[dict], Awaitable[None]]

_PREFIX = "sveta:bus"


class ChatBus:
    def __init__(self) -> None:
        self._subs: dict[str, ChatCallback] = {}
        self._counter = 0
        # routing key → set of subscription IDs
        self._keyed_subs: dict[str, set[str]] = {}
        # asyncio.Queue fed by the Redis subscriber task
        self._queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    # ── Subscribe / Unsubscribe ───────────────────────────────────────────────

    def subscribe(self, callback: ChatCallback, user_sub: str | None = None) -> str:
        self._counter += 1
        sid = str(self._counter)
        self._subs[sid] = callback
        if user_sub:
            self._keyed_subs.setdefault(user_sub, set()).add(sid)
        return sid

    def unsubscribe(self, sid: str, user_sub: str | None = None) -> None:
        self._subs.pop(sid, None)
        key = user_sub
        if key and key in self._keyed_subs:
            self._keyed_subs[key].discard(sid)
            if not self._keyed_subs[key]:
                del self._keyed_subs[key]

    def subscribe_room(self, room_id: str, callback: ChatCallback) -> str:
        return self.subscribe(callback, user_sub=f"room:{room_id}")

    def unsubscribe_room(self, room_id: str, sid: str) -> None:
        self.unsubscribe(sid, user_sub=f"room:{room_id}")

    # ── Publish (write path) ─────────────────────────────────────────────────

    async def publish(self, event: dict) -> None:
        """Broadcast to all subscribers (global channel)."""
        await _redis_publish(f"{_PREFIX}:global", event)

    async def push_to_user(self, user_sub: str, event: dict) -> None:
        """Send to a specific user's WebSocket connections."""
        await _redis_publish(f"{_PREFIX}:user:{user_sub}", event)

    async def push_to_room(self, room_id: str, event: dict) -> None:
        """Broadcast a room event to all room subscribers."""
        await _redis_publish(f"{_PREFIX}:room:{room_id}", event)

    # ── Local dispatch (called by subscriber task) ───────────────────────────

    def _dispatch_local(self, channel: str, event: dict) -> None:
        """Route an incoming Redis message to local callbacks."""
        if channel == f"{_PREFIX}:global":
            targets = list(self._subs.values())
        elif channel.startswith(f"{_PREFIX}:user:"):
            key = channel[len(f"{_PREFIX}:user:"):]
            sids = self._keyed_subs.get(key, set())
            targets = [self._subs[s] for s in sids if s in self._subs]
        elif channel.startswith(f"{_PREFIX}:room:"):
            key = "room:" + channel[len(f"{_PREFIX}:room:"):]
            sids = self._keyed_subs.get(key, set())
            targets = [self._subs[s] for s in sids if s in self._subs]
        else:
            return
        for cb in targets:
            asyncio.create_task(_safe_call(cb, event))


# ── Redis helpers ─────────────────────────────────────────────────────────────

async def _redis_publish(channel: str, event: dict) -> None:
    """Publish event to Redis channel. Falls back to local dispatch on error."""
    try:
        from app.utils.redis_client import get_async_redis
        await get_async_redis().publish(channel, json.dumps(event, default=str))
    except Exception as exc:
        logger.warning("chat_bus_redis_publish_failed", channel=channel, error=str(exc))
        # Fallback: dispatch directly (single-worker mode)
        chat_bus._dispatch_local(channel, event)


async def _safe_call(callback: ChatCallback, event: dict) -> None:
    try:
        await callback(event)
    except Exception:
        pass


# ── Background subscriber task ────────────────────────────────────────────────

async def start_redis_subscriber() -> asyncio.Task:
    """
    Launch a long-running task that subscribes to all sveta:bus:* channels
    and dispatches incoming messages to local callbacks.

    Call this once in the FastAPI lifespan after startup.
    Returns the task so the caller can cancel it on shutdown.
    """
    task = asyncio.create_task(_subscriber_loop(), name="chat_bus_redis_subscriber")
    return task


async def _subscriber_loop() -> None:
    from app.config import settings
    import redis.asyncio as aioredis

    while True:
        try:
            r = aioredis.from_url(settings.redis_url, decode_responses=True)
            pubsub = r.pubsub()
            await pubsub.psubscribe(f"{_PREFIX}:*")
            logger.info("chat_bus_redis_subscribed", pattern=f"{_PREFIX}:*")
            async for message in pubsub.listen():
                if message["type"] != "pmessage":
                    continue
                channel = message.get("channel", "")
                try:
                    event = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue

                # Canvas map cache invalidation (broadcast from capability_builder)
                if channel == f"{_PREFIX}:skill_reload":
                    try:
                        from app.ai.orchestrator import invalidate_canvas_map_cache
                        invalidate_canvas_map_cache()
                    except Exception:
                        pass
                    continue

                chat_bus._dispatch_local(channel, event)
        except asyncio.CancelledError:
            logger.info("chat_bus_redis_subscriber_cancelled")
            return
        except Exception as exc:
            logger.warning("chat_bus_redis_subscriber_error", error=str(exc))
            await asyncio.sleep(5)


# ── Singleton ─────────────────────────────────────────────────────────────────

chat_bus = ChatBus()
