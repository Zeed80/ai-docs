from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


@lru_cache(maxsize=1)
def _get_engine():
    return create_async_engine(
        settings.database_url,
        echo=settings.app_debug,
        pool_size=20,
        max_overflow=10,
    )


@lru_cache(maxsize=1)
def _get_session_factory():
    return async_sessionmaker(_get_engine(), class_=AsyncSession, expire_on_commit=False)


# Lazy properties for backward compatibility
class _EngineProxy:
    """Lazy proxy so importing this module doesn't require asyncpg."""

    @property
    def engine(self):
        return _get_engine()

    async def dispose(self):
        _get_engine.cache_clear()
        _get_session_factory.cache_clear()


_proxy = _EngineProxy()
engine = _proxy  # type: ignore[assignment]


async def get_db() -> AsyncIterator[AsyncSession]:
    factory = _get_session_factory()
    async with factory() as session:
        yield session
