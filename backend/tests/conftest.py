"""Test fixtures — uses a real PostgreSQL database.

Priority for DB URL:
  1. TEST_DATABASE_URL env var (explicit override, e.g. CI)
  2. Running stack at localhost:5432 with database aiworkspace_test
  3. testcontainers fallback (spins up postgres:16-alpine via Docker)

Each test wraps its work in a transaction that is rolled back on teardown,
so tests are fully isolated without dropping/recreating tables between runs.
"""

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.base import Base

# Force test-mode config. Never inherit a production / auth-enabled environment
# (e.g. when the suite is run inside the prod container): that flips API auth on
# and trips the fail-closed production guard, breaking unrelated tests with
# 401/403 and collection RuntimeErrors. Tests always run in test mode.
os.environ["APP_ENV"] = "test"
os.environ["AUTH_ENABLED"] = "false"

# Disable rate limiting; run Celery tasks synchronously (no broker needed)
os.environ["RATE_LIMIT_API_PER_MINUTE"] = "0"
os.environ["RATE_LIMIT_LOGIN_PER_MINUTE"] = "0"
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")

# Redis — только свой. По умолчанию приложение ходит в redis://localhost:6379,
# а на хосте этот порт публикует ЧУЖОЙ стек (china-key-learning): тесты
# настроек писали туда ai_config/agent_config, телеметрия — свои ключи. Внутри
# контейнера REDIS_URL указывает на боевой Redis стека. Поэтому адрес заменяется
# недоступным (любое непокрытое обращение падает сразу, а не пишет в чужое), а
# клиенты приложения — Redis в памяти на каждый тест (`_redis_in_memory`).
# Настоящий тестовый Redis — только явно: TEST_REDIS_URL.
_TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
os.environ["REDIS_URL"] = _TEST_REDIS_URL or "redis://127.0.0.1:1/0"

# ── DB URL resolution ──────────────────────────────────────────────────────────


def _resolve_db_url() -> tuple[str, str]:
    """Return (async_url, display_label) for the test database."""
    # 1. Explicit env var
    if url := os.environ.get("TEST_DATABASE_URL"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1), "env:TEST_DATABASE_URL"

    # 2. Running stack — try POSTGRES_HOST env (container) then localhost (host)
    pg_host = os.environ.get("POSTGRES_HOST", "localhost")
    pg_port = int(os.environ.get("POSTGRES_PORT", "5432"))
    pg_user = os.environ.get("POSTGRES_USER", "aiworkspace")
    pg_pass = os.environ.get("POSTGRES_PASSWORD", "changeme")
    pg_db = os.environ.get("POSTGRES_DB", "aiworkspace")
    test_db = pg_db + "_test"

    # Открытый порт сам по себе ничего не доказывает: на машине разработчика
    # на 5432 может слушать посторонний Postgres из другого проекта. Раньше
    # проверялся только факт TCP-соединения, поэтому запасной путь через
    # testcontainers не включался, и треть набора падала ошибкой авторизации
    # вместо тестов. Проверяем именно ту базу, которой собираемся пользоваться.
    if _can_connect_to(pg_host, pg_port, pg_user, pg_pass, test_db):
        url = f"postgresql+asyncpg://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{test_db}"
        return url, f"stack:{pg_host}:{pg_port}/{test_db}"

    # 3. testcontainers fallback
    return "__testcontainers__", "testcontainers:postgres:16-alpine"


def _can_connect_to(host: str, port: int, user: str, password: str, dbname: str) -> bool:
    """Действительно ли по этому адресу отвечает наша тестовая база."""
    import socket

    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        return False

    try:
        import asyncio

        import asyncpg
    except ImportError:  # драйвера нет — доверяем открытому порту, как раньше
        return True

    async def _probe() -> bool:
        try:
            conn = await asyncpg.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=dbname,
                timeout=3,
            )
        except Exception:
            return False
        await conn.close()
        return True

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


_DB_URL, _DB_LABEL = _resolve_db_url()


# ── Session-scoped container (only when testcontainers needed) ─────────────────


@pytest.fixture(scope="session")
def _pg_container():
    """Lazily start a PostgreSQL container only when the stack is unavailable."""
    if _DB_URL != "__testcontainers__":
        yield None
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        image="postgres:16-alpine", username="test", password="test", dbname="test_db"
    ) as pg:
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
async def client(db_session: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    from app.config import settings
    from app.db.session import get_db
    from app.main import app

    settings.rate_limit_api_per_minute = 0
    settings.rate_limit_login_per_minute = 0

    # Internal HTTP helpers open sessions without Depends(get_db). Keep those
    # writes inside the same test transaction, never the configured app database.
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    # Every internal session shares this test's single connection (above). A
    # background task still holding it when `db_session` rolls back makes asyncpg
    # raise "another operation is in progress" at teardown — an error that names
    # the fixture, not the endpoint that spawned the task. Let those tasks finish
    # first; whatever is still running after the grace period gets cancelled.
    await _drain_background_tasks()
    app.dependency_overrides.clear()


async def _drain_background_tasks(timeout: float = 5.0) -> None:
    import asyncio

    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if not pending:
        return
    done, still_running = await asyncio.wait(pending, timeout=timeout)
    for task in still_running:
        task.cancel()
    if still_running:
        await asyncio.gather(*still_running, return_exceptions=True)


@pytest.fixture(autouse=True)
def _generated_skills_go_to_tmp(tmp_path, monkeypatch):
    """CapabilityBuilder не должен писать черновики в исходники приложения.

    В тестах он их пишет: любой ход агента, упершийся в нехватку возможности,
    оставляет заглушку в ``app/ai/generated_skills``. Файлы накапливались от
    прогона к прогону, попадали в индекс с новым timestamp — и, что хуже,
    подхватывались следующими прогонами как настоящие скиллы. Из-за четырёх
    таких заглушек `test_secretary_answers_flow_status_directly` начинал
    уводить вопрос к исполнителю вместо прямого ответа: тест падал не там,
    где ломалось, а прогон замедлялся с 17 до 110 секунд.
    """
    root = tmp_path / "generated_skills"
    root.mkdir()
    monkeypatch.setattr("app.ai.capability_builder._GENERATED_ROOT", root)
    monkeypatch.setattr("app.api.dynamic_skill_runner._GENERATED_ROOT", root)
    return root


@pytest.fixture(autouse=True)
def _redis_in_memory(request, monkeypatch):
    """Клиенты Redis приложения — в памяти, свежие на каждый тест.

    Без fakeredis (образ backend без dev-зависимостей) остаётся недоступный
    REDIS_URL: обращение падает, но никуда не пишет.
    """
    # Тесты самих пулов подменяют их моками и в сеть не ходят; адрес всё равно
    # недоступный.
    if _TEST_REDIS_URL or request.node.get_closest_marker("real_redis_client"):
        yield
        return
    try:
        import fakeredis
        import fakeredis.aioredis
    except ImportError:
        yield
        return

    import asyncio

    class _NetworkLikeFakeRedis(fakeredis.aioredis.FakeRedis):
        """Команда отдаёт управление циклу, как настоящий сетевой вызов.

        fakeredis отвечает, не уступая циклу, и меняет порядок конкурентных
        корутин: test_concurrent_callers_refresh_the_token_only_once падал на
        порядке, которого с настоящим Redis не бывает.
        """

        async def execute_command(self, *args, **options):
            await asyncio.sleep(0)
            return await super().execute_command(*args, **options)

    server = fakeredis.FakeServer()

    def sync_client():
        return fakeredis.FakeRedis(server=server, decode_responses=True)

    def async_client():
        return _NetworkLikeFakeRedis(server=server, decode_responses=True)

    import app.utils.redis_client as redis_client

    monkeypatch.setattr(redis_client, "get_sync_redis", sync_client)
    monkeypatch.setattr(redis_client, "get_async_redis", async_client)
    monkeypatch.setattr(redis_client, "get_async_redis_pubsub", async_client)
    # Импорт на уровне модуля держит собственную ссылку на функцию.
    monkeypatch.setattr(
        "app.services.integration_config.get_sync_redis", sync_client, raising=False
    )
    monkeypatch.setattr(
        "app.ai.gpu_lock._redis", lambda: fakeredis.FakeRedis(server=server), raising=False
    )
    yield


@pytest.fixture(autouse=True)
def _settings_files_go_to_tmp(tmp_path, monkeypatch):
    """Файлы настроек и песочницы — во временный каталог теста.

    ``data/`` на хосте — рабочие копии разработчика, а в контейнере это
    боевой том (/app/data): тест PATCH /api/ai/config переписывал выбор моделей
    агента, конструктор возможностей копил каталоги в agent_sandbox.
    """
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("app.api.ai_settings._CONFIG_FILE", data / "ai_config.json")
    monkeypatch.setattr("app.ai.agent_config._CONFIG_FILE", data / "agent_config.json")
    monkeypatch.setattr("app.ai.capability_sandbox._ROOT", data / "agent_sandbox")
    monkeypatch.setattr("app.ai.capability_sandbox._STAGING_ROOT", data / "agent_staging")
