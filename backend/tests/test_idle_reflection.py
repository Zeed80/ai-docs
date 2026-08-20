"""Idle-reflection "subconscious" beat job (Ф6, AGENT_AUTONOMY_ROADMAP.md):
idle detection, duplicate-proposal consolidation, connector revalidation,
and the self-throttled entry point. Mirrors test_graph_analytics.py's
Redis-stub fixture pattern.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemoryFact, SourceConnector, User
from app.domain import idle_reflection as idr
from app.domain.idle_reflection import (
    IdleReflectionSettings,
    consolidate_duplicate_proposed_facts,
    is_system_idle,
    revalidate_due_connectors,
    run_idle_reflection,
    save_idle_reflection_settings,
)


@pytest.fixture(autouse=True)
def _mem_redis_settings(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(idr, "_redis_get_settings", lambda: dict(store) if store else None)

    def _set(value: dict) -> None:
        store.clear()
        store.update(value)

    monkeypatch.setattr(idr, "_redis_set_settings", _set)


@pytest.fixture(autouse=True)
async def _clean_tables(db_session: AsyncSession):
    yield
    await db_session.execute(delete(MemoryFact).where(
        MemoryFact.kind.in_(["idle_reflection_state", "proposed_fact"])
    ))
    await db_session.execute(delete(SourceConnector))
    await db_session.execute(delete(User))
    await db_session.commit()


def _user(sub: str, *, last_seen_at: datetime | None) -> User:
    return User(sub=sub, email=f"{sub}@example.com", name=sub, last_seen_at=last_seen_at)


def _proposed_fact(*, scope: str, title: str, summary: str, created_at: datetime) -> MemoryFact:
    fact = MemoryFact(
        scope=scope, kind="proposed_fact", title=title, summary=summary,
        source="memory_promotion", confidence=0.8, pinned=False, status="active",
        metadata_={"promotion_status": "pending"},
    )
    fact.created_at = created_at  # override TimestampMixin's default for ordering control
    return fact


# ── is_system_idle ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_system_idle_true_when_no_user_ever_logged_in(db_session: AsyncSession):
    assert await is_system_idle(db_session, threshold_minutes=30) is True


@pytest.mark.asyncio
async def test_is_system_idle_false_when_a_user_was_recently_seen(db_session: AsyncSession):
    db_session.add(_user("alice", last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    await db_session.commit()
    assert await is_system_idle(db_session, threshold_minutes=30) is False


@pytest.mark.asyncio
async def test_is_system_idle_true_when_last_seen_is_past_threshold(db_session: AsyncSession):
    db_session.add(_user("alice", last_seen_at=datetime.now(timezone.utc) - timedelta(hours=2)))
    await db_session.commit()
    assert await is_system_idle(db_session, threshold_minutes=30) is True


@pytest.mark.asyncio
async def test_is_system_idle_uses_the_most_recently_seen_of_several_users(db_session: AsyncSession):
    db_session.add(_user("alice", last_seen_at=datetime.now(timezone.utc) - timedelta(hours=2)))
    db_session.add(_user("bob", last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    await db_session.commit()
    assert await is_system_idle(db_session, threshold_minutes=30) is False


# ── consolidate_duplicate_proposed_facts ────────────────────────────────


@pytest.mark.asyncio
async def test_consolidate_keeps_newest_and_supersedes_older_duplicates(db_session: AsyncSession):
    now = datetime.now(timezone.utc)
    older = _proposed_fact(scope="project", title="Поставщик X сменил реквизиты",
                            summary="v1", created_at=now - timedelta(hours=2))
    newer = _proposed_fact(scope="project", title="Поставщик X сменил реквизиты",
                            summary="v2", created_at=now - timedelta(minutes=5))
    db_session.add_all([older, newer])
    await db_session.flush()

    consolidated = await consolidate_duplicate_proposed_facts(db_session)
    await db_session.commit()

    assert consolidated == 1
    await db_session.refresh(older)
    await db_session.refresh(newer)
    assert older.status == "superseded"
    assert older.superseded_by_id == newer.id
    assert newer.status == "active"
    assert newer.superseded_by_id is None


@pytest.mark.asyncio
async def test_consolidate_leaves_distinct_titles_and_scopes_untouched(db_session: AsyncSession):
    now = datetime.now(timezone.utc)
    a = _proposed_fact(scope="project", title="A", summary="a", created_at=now)
    b = _proposed_fact(scope="project", title="B", summary="b", created_at=now)
    c = _proposed_fact(scope="owner:alice", title="A", summary="a-alice", created_at=now)  # same title, different scope
    db_session.add_all([a, b, c])
    await db_session.flush()

    consolidated = await consolidate_duplicate_proposed_facts(db_session)
    await db_session.commit()

    assert consolidated == 0
    for fact in (a, b, c):
        await db_session.refresh(fact)
        assert fact.status == "active"


@pytest.mark.asyncio
async def test_consolidate_ignores_already_superseded_and_other_kinds(db_session: AsyncSession):
    now = datetime.now(timezone.utc)
    already = _proposed_fact(scope="project", title="Дубль", summary="old", created_at=now - timedelta(hours=1))
    already.status = "superseded"
    other_kind = MemoryFact(
        scope="project", kind="verified_fact", title="Дубль", summary="verified",
        source="memory_promotion", confidence=1.0, pinned=False, status="active",
    )
    db_session.add_all([already, other_kind])
    await db_session.flush()

    consolidated = await consolidate_duplicate_proposed_facts(db_session)
    await db_session.commit()

    assert consolidated == 0


@pytest.mark.asyncio
async def test_consolidate_handles_triple_duplicates(db_session: AsyncSession):
    now = datetime.now(timezone.utc)
    v1 = _proposed_fact(scope="project", title="T", summary="v1", created_at=now - timedelta(hours=3))
    v2 = _proposed_fact(scope="project", title="T", summary="v2", created_at=now - timedelta(hours=2))
    v3 = _proposed_fact(scope="project", title="T", summary="v3", created_at=now - timedelta(minutes=1))
    db_session.add_all([v1, v2, v3])
    await db_session.flush()

    consolidated = await consolidate_duplicate_proposed_facts(db_session)
    await db_session.commit()

    assert consolidated == 2
    await db_session.refresh(v1)
    await db_session.refresh(v2)
    await db_session.refresh(v3)
    assert v1.status == "superseded" and v1.superseded_by_id == v3.id
    assert v2.status == "superseded" and v2.superseded_by_id == v3.id
    assert v3.status == "active"


# ── revalidate_due_connectors ───────────────────────────────────────────


def _connector(*, domain: str, sample_url: str | None, revalidate_after: datetime | None,
                status: str = "retired") -> SourceConnector:
    return SourceConnector(
        domain_pattern=domain,
        strategy={"sample_url": sample_url, "queries": ["q"]} if sample_url else {},
        trigger_examples=["q"],
        status=status,
        success_count=1,
        fail_count=3,
        consecutive_failures=2,
        revalidate_after=revalidate_after,
    )


@pytest.mark.asyncio
async def test_revalidate_due_connector_success_revives_it(db_session: AsyncSession):
    connector = _connector(
        domain="revive.example", sample_url="https://revive.example/catalog",
        revalidate_after=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db_session.add(connector)
    await db_session.commit()

    from app.api.web_search import WebFetchResponse

    fetch_response = WebFetchResponse(
        url="https://revive.example/catalog", status=200,
        text="каталог инструмента " * 20, diagnostics=[],
    )
    with patch("app.api.web_search.fetch_page", new=AsyncMock(return_value=fetch_response)):
        result = await revalidate_due_connectors(db_session, batch_size=5)
    await db_session.commit()

    assert result["checked"] == 1
    assert result["revived_or_confirmed"] == 1
    await db_session.refresh(connector)
    assert connector.status == "draft"  # retired -> draft revival
    assert connector.consecutive_failures == 0


@pytest.mark.asyncio
async def test_revalidate_due_connector_failure_keeps_backing_off(db_session: AsyncSession):
    connector = _connector(
        domain="stillbad.example", sample_url="https://stillbad.example/x",
        revalidate_after=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db_session.add(connector)
    await db_session.commit()

    with patch("app.api.web_search.fetch_page", new=AsyncMock(side_effect=RuntimeError("timeout"))):
        result = await revalidate_due_connectors(db_session, batch_size=5)
    await db_session.commit()

    assert result["still_failing"] == 1
    await db_session.refresh(connector)
    assert connector.status == "retired"  # stays retired
    assert connector.consecutive_failures == 3


@pytest.mark.asyncio
async def test_revalidate_skips_connector_without_sample_url(db_session: AsyncSession):
    connector = _connector(
        domain="nourls.example", sample_url=None,
        revalidate_after=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db_session.add(connector)
    await db_session.commit()

    result = await revalidate_due_connectors(db_session, batch_size=5)

    assert result["skipped_no_url"] == 1
    assert result["checked"] == 0


@pytest.mark.asyncio
async def test_revalidate_ignores_connector_not_yet_due(db_session: AsyncSession):
    connector = _connector(
        domain="notyet.example", sample_url="https://notyet.example/x",
        revalidate_after=datetime.now(timezone.utc) + timedelta(days=5),
    )
    db_session.add(connector)
    await db_session.commit()

    result = await revalidate_due_connectors(db_session, batch_size=5)

    assert result["checked"] == 0


@pytest.mark.asyncio
async def test_revalidate_respects_batch_size(db_session: AsyncSession):
    now = datetime.now(timezone.utc)
    for i in range(3):
        db_session.add(_connector(
            domain=f"many{i}.example", sample_url=f"https://many{i}.example/x",
            revalidate_after=now - timedelta(minutes=1),
        ))
    await db_session.commit()

    with patch("app.api.web_search.fetch_page", new=AsyncMock(side_effect=RuntimeError("x"))):
        result = await revalidate_due_connectors(db_session, batch_size=2)

    assert result["checked"] == 2


# ── run_idle_reflection: throttle/gating ─────────────────────────────────


@pytest.mark.asyncio
async def test_run_idle_reflection_skips_when_disabled(db_session: AsyncSession):
    save_idle_reflection_settings(IdleReflectionSettings(enabled=False))
    result = await run_idle_reflection(db_session, force=False)
    assert result == {"skipped": True, "reason": "disabled"}


@pytest.mark.asyncio
async def test_run_idle_reflection_skips_when_user_active(db_session: AsyncSession):
    db_session.add(_user("alice", last_seen_at=datetime.now(timezone.utc)))
    await db_session.commit()
    result = await run_idle_reflection(db_session, force=False)
    assert result == {"skipped": True, "reason": "user_active"}


@pytest.mark.asyncio
async def test_run_idle_reflection_runs_when_idle_and_reports_work_done(db_session: AsyncSession):
    now = datetime.now(timezone.utc)
    older = _proposed_fact(scope="project", title="Дубль", summary="v1", created_at=now - timedelta(hours=1))
    newer = _proposed_fact(scope="project", title="Дубль", summary="v2", created_at=now)
    db_session.add_all([older, newer])
    await db_session.commit()

    result = await run_idle_reflection(db_session, force=False)
    await db_session.commit()

    assert result["skipped"] is False
    assert result["consolidated_facts"] == 1
    assert result["connector_revalidation"]["checked"] == 0


@pytest.mark.asyncio
async def test_run_idle_reflection_respects_min_interval_between_runs(db_session: AsyncSession):
    first = await run_idle_reflection(db_session, force=False)
    await db_session.commit()
    assert first["skipped"] is False

    second = await run_idle_reflection(db_session, force=False)
    assert second == {"skipped": True, "reason": "interval_not_elapsed"}


@pytest.mark.asyncio
async def test_run_idle_reflection_force_bypasses_every_gate(db_session: AsyncSession):
    db_session.add(_user("alice", last_seen_at=datetime.now(timezone.utc)))
    save_idle_reflection_settings(IdleReflectionSettings(enabled=False))
    await db_session.commit()

    result = await run_idle_reflection(db_session, force=True)
    await db_session.commit()

    assert result["skipped"] is False
