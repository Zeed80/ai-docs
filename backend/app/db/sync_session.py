"""Shared synchronous engine/session for Celery tasks.

Celery tasks run outside the async request path and each used to build its own
``create_engine(...)`` per call — a fresh pool (and TCP handshake) for a query
that returns a handful of rows, on a task that fires every minute. This module
keeps one pooled engine per worker process, mirroring what
``app.tasks.extraction`` already does locally.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import settings


@lru_cache(maxsize=1)
def get_sync_engine() -> Engine:
    return create_engine(
        settings.database_url_sync,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
    )


def sync_session() -> Session:
    """A Session on the process-wide engine. Use as a context manager."""
    return Session(get_sync_engine())
