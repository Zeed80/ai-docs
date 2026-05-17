"""Test fixtures for backend API tests."""

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base

# SQLite for tests — no asyncpg needed
TEST_DB_URL = "sqlite+aiosqlite:///./test.db"

# Disable rate limiting in tests (must happen before app.config is imported)
os.environ["RATE_LIMIT_API_PER_MINUTE"] = "0"
os.environ["RATE_LIMIT_LOGIN_PER_MINUTE"] = "0"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    # Import models to register them with Base.metadata
    from app.db import models  # noqa: F401
    from app.db.models import FileExtensionAllowlist

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            FileExtensionAllowlist.__table__.insert(),
            [
                {"extension": ".pdf", "is_allowed": True, "added_by": "test"},
                {"extension": ".txt", "is_allowed": True, "added_by": "test"},
                {"extension": ".docx", "is_allowed": True, "added_by": "test"},
                {"extension": ".xlsx", "is_allowed": True, "added_by": "test"},
            ],
        )
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncIterator[AsyncSession]:
    # Each test gets its own connection + transaction rolled back at teardown
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

    # Ensure rate limiting is off regardless of when settings was created
    settings.rate_limit_api_per_minute = 0
    settings.rate_limit_login_per_minute = 0

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
