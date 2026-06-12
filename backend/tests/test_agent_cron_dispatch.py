"""AgentCron executor: cron matching and headless dispatch."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AgentCron, AgentTask
from app.tasks import agent_cron

# ── Cron matcher ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "schedule,moment,expected",
    [
        ("* * * * *", datetime(2026, 6, 12, 10, 30, tzinfo=UTC), True),
        ("30 2 * * *", datetime(2026, 6, 12, 2, 30, tzinfo=UTC), True),
        ("30 2 * * *", datetime(2026, 6, 12, 2, 31, tzinfo=UTC), False),
        ("*/15 * * * *", datetime(2026, 6, 12, 10, 45, tzinfo=UTC), True),
        ("*/15 * * * *", datetime(2026, 6, 12, 10, 50, tzinfo=UTC), False),
        ("0 9-18 * * *", datetime(2026, 6, 12, 12, 0, tzinfo=UTC), True),
        ("0 9-18 * * *", datetime(2026, 6, 12, 20, 0, tzinfo=UTC), False),
        # 2026-06-12 is a Friday (cron dow: 5).
        ("0 10 * * 5", datetime(2026, 6, 12, 10, 0, tzinfo=UTC), True),
        ("0 10 * * 1", datetime(2026, 6, 12, 10, 0, tzinfo=UTC), False),
        # Sunday=0 in cron; 2026-06-14 is a Sunday.
        ("0 10 * * 0", datetime(2026, 6, 14, 10, 0, tzinfo=UTC), True),
        ("0 10 1,15 * *", datetime(2026, 6, 15, 10, 0, tzinfo=UTC), True),
        ("0 10 1,15 * *", datetime(2026, 6, 14, 10, 0, tzinfo=UTC), False),
        # Malformed → never due.
        ("nonsense", datetime(2026, 6, 12, 10, 0, tzinfo=UTC), False),
        ("* * *", datetime(2026, 6, 12, 10, 0, tzinfo=UTC), False),
    ],
)
def test_cron_matches(schedule, moment, expected):
    assert agent_cron.cron_matches(schedule, moment) is expected


def test_is_due_runs_once_per_minute():
    now = datetime(2026, 6, 12, 2, 30, 40, tzinfo=UTC)
    assert agent_cron._is_due("30 2 * * *", None, now) is True
    # Already ran this minute → not due again.
    ran = datetime(2026, 6, 12, 2, 30, 5, tzinfo=UTC)
    assert agent_cron._is_due("30 2 * * *", ran, now) is False
    # Ran yesterday → due.
    yesterday = datetime(2026, 6, 11, 2, 30, tzinfo=UTC)
    assert agent_cron._is_due("30 2 * * *", yesterday, now) is True


# ── Dispatch ───────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def cron_db(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    import app.db.session as session_module
    monkeypatch.setattr(session_module, "_get_session_factory", lambda: factory)
    yield factory
    async with factory() as db:
        from sqlalchemy import delete
        await db.execute(delete(AgentTask))
        await db.execute(delete(AgentCron))
        await db.commit()


@pytest.mark.asyncio
async def test_dispatch_runs_due_cron_and_records_task(cron_db, monkeypatch):
    async with cron_db() as db:
        db.add(AgentCron(schedule="* * * * *", prompt="дай сводку дня", enabled=True))
        db.add(AgentCron(schedule="0 0 1 1 *", prompt="новогодний отчёт", enabled=True))
        db.add(AgentCron(schedule="* * * * *", prompt="выключен", enabled=False))
        await db.commit()

    async def fake_turn(prompt: str):
        return True, f"Готово: {prompt}"

    monkeypatch.setattr(agent_cron, "_run_headless_turn", fake_turn)

    executed = await agent_cron._dispatch()
    assert executed == 1

    async with cron_db() as db:
        from sqlalchemy import select
        tasks = list((await db.execute(select(AgentTask))).scalars().all())
        assert len(tasks) == 1
        assert tasks[0].status == "completed"
        assert "дай сводку дня" in (tasks[0].output or "")
        crons = list((await db.execute(select(AgentCron))).scalars().all())
        ran = next(c for c in crons if c.prompt == "дай сводку дня")
        assert ran.run_count == 1 and ran.last_run_at is not None

    # Second dispatch within the same minute → nothing runs again.
    executed = await agent_cron._dispatch()
    assert executed == 0


@pytest.mark.asyncio
async def test_dispatch_records_failed_turn(cron_db, monkeypatch):
    async with cron_db() as db:
        db.add(AgentCron(schedule="* * * * *", prompt="сломанная задача", enabled=True))
        await db.commit()

    async def failing_turn(prompt: str):
        return False, "[errors] llm down"

    monkeypatch.setattr(agent_cron, "_run_headless_turn", failing_turn)

    executed = await agent_cron._dispatch()
    assert executed == 1
    async with cron_db() as db:
        from sqlalchemy import select
        task = (await db.execute(select(AgentTask))).scalars().one()
        assert task.status == "failed"


def test_beat_schedule_contains_dispatcher():
    from app.tasks.celery_app import celery_app
    entry = celery_app.conf.beat_schedule.get("agent-cron-dispatch")
    assert entry and entry["task"] == "agent.cron_dispatch"
    assert entry["schedule"] == 60.0
