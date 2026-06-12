"""Persistent per-process event loop for async Celery tasks.

Async SQLAlchemy's connection pool binds connections to the event loop that
created them. ``asyncio.run()`` builds a fresh loop per task, so the second
task that touches the cached engine gets pooled connections from a dead loop
("attached to a different loop"). The legacy ``asyncio.get_event_loop()``
pattern kept one loop alive but crashes with "no current event loop" whenever
something else on the worker has already run ``asyncio.run``.

:func:`run_async` keeps ONE loop per worker process (fork): pooled
connections stay valid across tasks, and the loop is (re)created when missing
or closed. Use it for every async Celery task that touches the database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run *coro* on this worker process's persistent event loop."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop.run_until_complete(coro)
