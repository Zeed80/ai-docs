"""In-memory pub/sub bus for broadcasting chat events to WebSocket clients.

Used to mirror Telegram conversations into the web chat UI in real time.
Only Telegram→Web mirroring is supported; web chat messages are NOT
forwarded to Telegram unless the user explicitly requests it.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Awaitable

logger = logging.getLogger(__name__)

ChatCallback = Callable[[dict], Awaitable[None]]


class ChatBus:
    def __init__(self) -> None:
        self._subs: dict[str, ChatCallback] = {}
        self._counter = 0

    def subscribe(self, callback: ChatCallback) -> str:
        self._counter += 1
        sid = str(self._counter)
        self._subs[sid] = callback
        return sid

    def unsubscribe(self, sid: str) -> None:
        self._subs.pop(sid, None)

    async def publish(self, event: dict) -> None:
        for callback in list(self._subs.values()):
            asyncio.create_task(_safe_call(callback, event))


async def _safe_call(callback: ChatCallback, event: dict) -> None:
    try:
        await callback(event)
    except Exception:
        pass


chat_bus = ChatBus()
