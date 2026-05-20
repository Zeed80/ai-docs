"""Test fixtures — uses a real PostgreSQL database.

Priority for DB URL:
  1. TEST_DATABASE_URL env var (explicit override, e.g. CI)
  2. Running stack at localhost:5432 with database aiworkspace_test
  3. testcontainers fallback (spins up postgres:16-alpine via Docker)

Each test wraps its work in a transaction that is rolled back on teardown,
so tests are fully isolated without dropping/recreating tables between runs.
"""

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.base import Base

# Disable rate limiting; run Celery tasks synchronously (no broker needed)
os.environ["RATE_LIMIT_API_PER_MINUTE"] = "0"
os.environ["RATE_LIMIT_LOGIN_PER_MINUTE"] = "0"
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")

# ── DB URL resolution ──────────────────────────────────────────────────────────

_STACK_URL = (
    "postgresql+asyncpg://aiworkspace:changeme@localhost:5432/aiworkspace_test"
)

def _resolve_db_url() -> tuple[str, str]:
    """Return (async_url, display_label) for the test database."""
    # 1. Explicit env var
    if url := os.environ.get("TEST_DATABASE_URL"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1), "env:TEST_DATABASE_URL"

    # 2. Running stack — quick sync check
    import socket
    try:
        s = socket.create_connection(("localhost", 5432), timeout=1)
        s.close()
        return _STACK_URL, "stack:localhost:5432/aiworkspace_test"
    except OSError:
        pass

    # 3. testcontainers fallback
    return "__testcontainers__", "testcontainers:postgres:16-alpine"


_DB_URL, _DB_LABEL = _resolve_db_url()


# ── Session-scoped container (only when testcontainers needed) ─────────────────

@pytest.fixture(scope="session")
def _pg_container():
    """Lazily start a PostgreSQL container only when the stack is unavailable."""
    if _DB_URL != "__testcontainers__":
        yield None
        return
    from testcontainers.postgres import PostgresContainer
    with PostgresContainer(image="postgres:16-alpine", username="test",
                           password="test", dbname="test_db") as pg:
        yield pg


# ── Engine ─────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session")
async def test_engine(_pg_container):
    """One engine for the whole test session; schema created once."""
    if _DB_URL == "__testcontainers__":
        raw = _pg_container.get_connection_url()
        url = raw.replace("psycopg2", "asyncpg", 1)
        if "asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    else:
        url = _DB_URL

    print(f"\n[conftest] Using DB: {_DB_LABEL}")
    engine = create_async_engine(url, echo=False, poolclass=NullPool)

    from app.db import models  # noqa: F401
    from app.db.models import FileExtensionAllowlist

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            FileExtensionAllowlist.__table__.insert(),
            [
                {"extension": ".pdf",  "is_allowed": True, "added_by": "test"},
                {"extension": ".txt",  "is_allowed": True, "added_by": "test"},
                {"extension": ".docx", "is_allowed": True, "added_by": "test"},
                {"extension": ".xlsx", "is_allowed": True, "added_by": "test"},
            ],
        )

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ── Per-test transaction isolation ────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncIterator[AsyncSession]:
    """Each test: open a connection, begin a transaction, yield session, rollback."""
    async with test_engine.connect() as conn:
        await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await conn.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    from app.config import settings
    from app.db.session import get_db
    from app.main import app

    settings.rate_limit_api_per_minute = 0
    settings.rate_limit_login_per_minute = 0

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
