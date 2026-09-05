"""Source connector lifecycle (Ф5, AGENT_AUTONOMY_ROADMAP.md): record ->
retrieve -> outcome stats -> activate/retire/revalidate.

Mirrors test_recipe_skills.py's structure and monkeypatch pattern (stub the
vector layer + point _get_session_factory at the test DB) — same rationale:
record_connector_outcome/find_connector_hints open their own session via
_get_session_factory, while record_connector_success/record_connector_failure
take the caller's session directly (mirroring how web_discover calls them).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import connectors
from app.db.models import SourceConnector


@pytest_asyncio.fixture
async def connectors_db(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    import app.db.session as session_module

    monkeypatch.setattr(session_module, "_get_session_factory", lambda: factory)

    indexed: list[dict] = []

    async def fake_index(connector_id, example_idx, text):
        indexed.append({"connector_id": connector_id, "idx": example_idx, "text": text})

    search_results: list[dict] = []

    async def fake_search(text, limit=3):
        return list(search_results)

    monkeypatch.setattr(connectors, "_index_trigger", fake_index)
    monkeypatch.setattr(connectors, "_search_triggers", fake_search)
    monkeypatch.setattr(connectors, "capabilities_schema_hash", lambda: "hash-v1")

    yield {"factory": factory, "indexed": indexed, "search_results": search_results}

    async with factory() as db:
        from sqlalchemy import delete

        await db.execute(delete(SourceConnector))
        await db.commit()


@pytest.mark.asyncio
async def test_record_success_creates_draft(connectors_db):
    async with connectors_db["factory"]() as db:
        connector_id = await connectors.record_connector_success(
            db, url="https://haltec.ru/catalog", queries=["haltec сверла каталог"]
        )
    assert connector_id is not None

    async with connectors_db["factory"]() as db:
        connector = await db.get(SourceConnector, uuid.UUID(connector_id))
    assert connector.status == "draft"
    assert connector.domain_pattern == "haltec.ru"
    assert connector.schema_hash == "hash-v1"
    assert connector.success_count == 1
    assert connector.strategy["sample_url"] == "https://haltec.ru/catalog"
    assert connectors_db["indexed"], "trigger example must be indexed for retrieval"


@pytest.mark.asyncio
async def test_record_success_strips_www_and_dedupes_by_domain(connectors_db):
    async with connectors_db["factory"]() as db:
        first_id = await connectors.record_connector_success(
            db, url="https://www.haltec.ru/catalog", queries=["haltec сверла"]
        )
    async with connectors_db["factory"]() as db:
        second_id = await connectors.record_connector_success(
            db, url="https://haltec.ru/other-page", queries=["haltec фрезы"]
        )
    assert first_id == second_id

    async with connectors_db["factory"]() as db:
        connector = await db.get(SourceConnector, uuid.UUID(first_id))
    assert connector.success_count == 2
    assert set(connector.trigger_examples) == {"haltec сверла", "haltec фрезы"}
    assert connector.strategy["sample_url"] == "https://haltec.ru/other-page"


@pytest.mark.asyncio
async def test_repeated_success_activates_draft(connectors_db):
    for _ in range(connectors._CONNECTOR_ACTIVATE_AFTER):
        async with connectors_db["factory"]() as db:
            connector_id = await connectors.record_connector_success(
                db, url="https://betar.ru/catalog", queries=["betar фрезы каталог"]
            )
    async with connectors_db["factory"]() as db:
        connector = await db.get(SourceConnector, uuid.UUID(connector_id))
    assert connector.status == "active"
    assert connector.success_count == connectors._CONNECTOR_ACTIVATE_AFTER
    assert connector.last_validated_at is not None


@pytest.mark.asyncio
async def test_failure_on_unknown_domain_creates_nothing(connectors_db):
    async with connectors_db["factory"]() as db:
        await connectors.record_connector_failure(db, url="https://unknown-supplier.example/x")
    async with connectors_db["factory"]() as db:
        from sqlalchemy import func, select

        count = (await db.execute(select(func.count()).select_from(SourceConnector))).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_failure_updates_freshness_fields_and_backs_off(connectors_db):
    async with connectors_db["factory"]() as db:
        connector_id = await connectors.record_connector_success(
            db, url="https://yg1.com/catalog", queries=["yg1 инструмент каталог"]
        )
    async with connectors_db["factory"]() as db:
        await connectors.record_connector_failure(db, url="https://yg1.com/other")
    async with connectors_db["factory"]() as db:
        connector = await db.get(SourceConnector, uuid.UUID(connector_id))
    assert connector.fail_count == 1
    assert connector.consecutive_failures == 1
    assert connector.last_failure_at is not None
    assert connector.revalidate_after is not None
    assert connector.revalidate_after - connector.last_failure_at <= timedelta(days=1, minutes=1)
    assert connectors.due_for_revalidation(connector) is False  # revalidate_after is in the future

    # A second consecutive failure backs off further (day -> week).
    async with connectors_db["factory"]() as db:
        await connectors.record_connector_failure(db, url="https://yg1.com/third")
    async with connectors_db["factory"]() as db:
        connector = await db.get(SourceConnector, uuid.UUID(connector_id))
    assert connector.consecutive_failures == 2
    assert connector.revalidate_after - connector.last_failure_at >= timedelta(days=6)


@pytest.mark.asyncio
async def test_success_resets_failure_streak(connectors_db):
    async with connectors_db["factory"]() as db:
        connector_id = await connectors.record_connector_success(
            db, url="https://reliable.example/catalog", queries=["reliable каталог"]
        )
    async with connectors_db["factory"]() as db:
        await connectors.record_connector_failure(db, url="https://reliable.example/x")
    async with connectors_db["factory"]() as db:
        connector = await db.get(SourceConnector, uuid.UUID(connector_id))
    assert connector.consecutive_failures == 1

    async with connectors_db["factory"]() as db:
        await connectors.record_connector_success(
            db, url="https://reliable.example/catalog", queries=["reliable каталог 2"]
        )
    async with connectors_db["factory"]() as db:
        connector = await db.get(SourceConnector, uuid.UUID(connector_id))
    assert connector.consecutive_failures == 0
    assert connector.last_failure_at is None
    assert connector.revalidate_after is None


@pytest.mark.asyncio
async def test_fail_rate_retires_connector(connectors_db):
    async with connectors_db["factory"]() as db:
        connector_id = await connectors.record_connector_success(
            db, url="https://flaky.example/catalog", queries=["flaky каталог"]
        )
    # 3 failures over 4 total uses (1 success + 3 fails) = 75% > 50% retire threshold,
    # with total (4) meeting _CONNECTOR_RETIRE_MIN_USES.
    for _ in range(3):
        async with connectors_db["factory"]() as db:
            await connectors.record_connector_failure(db, url="https://flaky.example/x")
    async with connectors_db["factory"]() as db:
        connector = await db.get(SourceConnector, uuid.UUID(connector_id))
    assert connector.status == "retired"


@pytest.mark.asyncio
async def test_success_after_retirement_creates_a_fresh_draft_not_a_revival(connectors_db):
    """record_connector_success's dedupe lookup excludes retired connectors on
    purpose (see its own docstring) — a domain that comes back online gets a
    new draft, the old retired row is left alone as history."""
    async with connectors_db["factory"]() as db:
        connector_id = await connectors.record_connector_success(
            db, url="https://flaky2.example/catalog", queries=["flaky2 каталог"]
        )
    for _ in range(3):
        async with connectors_db["factory"]() as db:
            await connectors.record_connector_failure(db, url="https://flaky2.example/x")
    async with connectors_db["factory"]() as db:
        old = await db.get(SourceConnector, uuid.UUID(connector_id))
        assert old.status == "retired"

    async with connectors_db["factory"]() as db:
        new_id = await connectors.record_connector_success(
            db, url="https://flaky2.example/catalog", queries=["flaky2 каталог снова"]
        )
    assert new_id != connector_id
    async with connectors_db["factory"]() as db:
        old = await db.get(SourceConnector, uuid.UUID(connector_id))
        new = await db.get(SourceConnector, uuid.UUID(new_id))
    assert old.status == "retired"
    assert new.status == "draft"


@pytest.mark.asyncio
async def test_record_connector_outcome_revives_a_retired_connector_directly(connectors_db):
    """The revival path is unreachable from record_connector_success (see
    previous test) — it's for Ф6's idle-reflection job calling
    record_connector_outcome directly on a specific (retired) connector_id."""
    connector = SourceConnector(
        domain_pattern="revive.example",
        strategy={"queries": ["x"]},
        trigger_examples=["x"],
        status="retired",
        success_count=1,
        fail_count=3,
    )
    async with connectors_db["factory"]() as db:
        db.add(connector)
        await db.commit()
        await db.refresh(connector)
        cid = connector.id

    await connectors.record_connector_outcome(cid, success=True)

    async with connectors_db["factory"]() as db:
        revived = await db.get(SourceConnector, cid)
    assert revived.status == "draft"


@pytest.mark.asyncio
async def test_find_active_connector_exact_match_only_active(connectors_db):
    async with connectors_db["factory"]() as db:
        draft_id = await connectors.record_connector_success(
            db, url="https://drafted.example/catalog", queries=["drafted каталог"]
        )
    hit = await connectors.find_active_connector("drafted.example")
    assert hit is None  # still draft, not active yet

    for _ in range(connectors._CONNECTOR_ACTIVATE_AFTER - 1):
        async with connectors_db["factory"]() as db:
            await connectors.record_connector_success(
                db, url="https://drafted.example/catalog", queries=["drafted каталог"]
            )
    hit = await connectors.find_active_connector("drafted.example")
    assert hit is not None
    assert str(hit.id) == draft_id

    assert await connectors.find_active_connector("no-such-domain.example") is None


@pytest.mark.asyncio
async def test_find_connector_hints_skips_retired_and_uses_search_stub(connectors_db):
    async with connectors_db["factory"]() as db:
        connector_id = await connectors.record_connector_success(
            db, url="https://hinted.example/catalog", queries=["режущий инструмент каталог"]
        )
    connectors_db["search_results"].append({"connector_id": connector_id, "score": 0.8})

    hints = await connectors.find_connector_hints("найди каталог режущего инструмента")

    assert len(hints) == 1
    assert hints[0]["domain_pattern"] == "hinted.example"
    assert hints[0]["score"] == 0.8

    for _ in range(3):
        async with connectors_db["factory"]() as db:
            await connectors.record_connector_failure(db, url="https://hinted.example/x")
    async with connectors_db["factory"]() as db:
        connector = await db.get(SourceConnector, uuid.UUID(connector_id))
    assert connector.status == "retired"

    hints_after_retire = await connectors.find_connector_hints("найди каталог режущего инструмента")
    assert hints_after_retire == []


def test_due_for_revalidation_none_and_future_and_past():
    class _C:
        revalidate_after = None

    assert connectors.due_for_revalidation(_C()) is False

    class _Future:
        revalidate_after = datetime.now(UTC) + timedelta(days=1)

    assert connectors.due_for_revalidation(_Future()) is False

    class _Past:
        revalidate_after = datetime.now(UTC) - timedelta(minutes=1)

    assert connectors.due_for_revalidation(_Past()) is True


def test_domain_from_url_strips_www_and_lowercases():
    assert connectors._domain_from_url("https://WWW.Example.COM/path") == "example.com"
    assert connectors._domain_from_url("https://example.com") == "example.com"
    assert connectors._domain_from_url("not a url") is None
