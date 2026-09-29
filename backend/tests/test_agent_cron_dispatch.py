"""AgentCron dispatch: durable intake, ownership and occurrence replay."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.agent_runtime_models import DelegationGrant, DurableChatRun
from app.db.models import AgentCron, AgentTask, ChatSession, User, WorkOrder
from app.tasks import agent_cron


@pytest.mark.parametrize(
    "schedule,moment,expected",
    [
        ("* * * * *", datetime(2026, 6, 12, 10, 30, tzinfo=UTC), True),
        ("30 2 * * *", datetime(2026, 6, 12, 2, 30, tzinfo=UTC), True),
        ("*/15 * * * *", datetime(2026, 6, 12, 10, 45, tzinfo=UTC), True),
        ("0 10 * * 5", datetime(2026, 6, 12, 10, 0, tzinfo=UTC), True),
        ("nonsense", datetime(2026, 6, 12, 10, 0, tzinfo=UTC), False),
    ],
)
def test_cron_matches(schedule, moment, expected):
    assert agent_cron.cron_matches(schedule, moment) is expected


def test_is_due_runs_once_per_minute():
    now = datetime(2026, 6, 12, 2, 30, 40, tzinfo=UTC)
    assert agent_cron._is_due("30 2 * * *", None, now) is True
    assert (
        agent_cron._is_due("30 2 * * *", datetime(2026, 6, 12, 2, 30, 5, tzinfo=UTC), now) is False
    )


@pytest_asyncio.fixture
async def cron_db(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    async with factory() as db:
        prior_cron_ids = set((await db.scalars(select(AgentCron.id))).all())
    yield factory
    async with factory() as db:
        cron_ids = set((await db.scalars(select(AgentCron.id))).all()) - prior_cron_ids
        await db.execute(
            delete(DurableChatRun).where(DurableChatRun.owner_key.like("cron-owner-%"))
        )
        await db.execute(delete(WorkOrder).where(WorkOrder.owner_key.like("cron-owner-%")))
        sessions = (
            await db.scalars(select(ChatSession).where(ChatSession.user_key.like("cron-owner-%")))
        ).all()
        for session in sessions:
            await db.delete(session)
        tasks = (await db.scalars(select(AgentTask))).all()
        for task in tasks:
            cron_id = (task.metadata_ or {}).get("agent_cron_id")
            if cron_id and cron_id in {str(item) for item in cron_ids}:
                await db.delete(task)
        if cron_ids:
            await db.execute(delete(AgentCron).where(AgentCron.id.in_(cron_ids)))
        await db.execute(
            delete(DelegationGrant).where(DelegationGrant.owner_key.like("cron-owner-%"))
        )
        await db.execute(delete(User).where(User.sub.like("cron-owner-%")))
        await db.commit()


async def _add_active_owner(db, sub="cron-owner-active"):
    db.add(
        User(
            sub=sub,
            email=f"{sub}@example.test",
            name=sub,
            preferred_username=sub,
            role="admin",
            is_active=True,
        )
    )
    await db.flush()


async def _add_grant(db, owner_key):
    schedule = "* * * * *"
    prompt = "дай сводку дня"
    grant = DelegationGrant(
        owner_key=owner_key,
        title="cron test grant",
        actions=["agent.cron.run"],
        constraints={
            "schedule": schedule,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        },
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(grant)
    await db.flush()
    return grant


@pytest.mark.asyncio
async def test_two_beat_dispatchers_and_restart_create_one_durable_occurrence(cron_db):
    async with cron_db() as db:
        await _add_active_owner(db)
        grant = await _add_grant(db, "cron-owner-active")
        db.add(
            AgentCron(
                schedule="* * * * *",
                prompt="дай сводку дня",
                owner_key="cron-owner-active",
                delegation_grant_id=grant.id,
            )
        )
        await db.commit()

    now = datetime(2026, 6, 12, 10, 30, 40, tzinfo=UTC)
    first, second = await asyncio.gather(agent_cron._dispatch(now), agent_cron._dispatch(now))
    assert first + second == 1
    assert await agent_cron._dispatch(now) == 0

    async with cron_db() as db:
        tasks = [
            task
            for task in (await db.scalars(select(AgentTask))).all()
            if (task.metadata_ or {}).get("agent_cron_id")
        ]
        runs = list(
            (
                await db.scalars(
                    select(DurableChatRun).where(DurableChatRun.owner_key == "cron-owner-active")
                )
            ).all()
        )
        assert len(tasks) == len(runs) == 1
        assert tasks[0].status == "created"
        assert runs[0].intake_channel == "cron"
        assert runs[0].owner_key == "cron-owner-active"
        assert runs[0].external_message_id.endswith("2026-06-12T10:30:00+00:00")


@pytest.mark.asyncio
async def test_missed_schedule_is_not_backfilled_and_next_date_is_distinct(cron_db):
    async with cron_db() as db:
        await _add_active_owner(db)
        schedule = "30 2 * * *"
        prompt = "ежедневная сводка"
        grant = DelegationGrant(
            owner_key="cron-owner-active",
            title="daily cron grant",
            actions=["agent.cron.run"],
            constraints={
                "schedule": schedule,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            },
            max_actions=3,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(grant)
        await db.flush()
        db.add(
            AgentCron(
                schedule=schedule,
                prompt=prompt,
                owner_key="cron-owner-active",
                delegation_grant_id=grant.id,
            )
        )
        await db.commit()

    assert await agent_cron._dispatch(datetime(2026, 6, 12, 2, 31, tzinfo=UTC)) == 0
    assert await agent_cron._dispatch(datetime(2026, 6, 13, 2, 30, tzinfo=UTC)) == 1
    assert await agent_cron._dispatch(datetime(2026, 6, 14, 2, 30, tzinfo=UTC)) == 1
    async with cron_db() as db:
        runs = list(
            (
                await db.scalars(
                    select(DurableChatRun).where(DurableChatRun.owner_key == "cron-owner-active")
                )
            ).all()
        )
        assert len(runs) == 2
        assert len({run.external_message_id for run in runs}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_key", [None, "missing-owner", "cron-owner-revoked"])
async def test_unknown_or_revoked_owner_fails_closed(cron_db, owner_key):
    async with cron_db() as db:
        if owner_key == "cron-owner-revoked":
            await _add_active_owner(db, owner_key)
            grant = await _add_grant(db, owner_key)
            owner = await db.scalar(select(User).where(User.sub == owner_key))
            owner.is_active = False
        else:
            grant = None
        db.add(
            AgentCron(
                schedule="* * * * *",
                prompt="не запускать",
                owner_key=owner_key,
                delegation_grant_id=grant.id if grant else None,
            )
        )
        await db.commit()

    assert await agent_cron._dispatch(datetime(2026, 6, 12, 10, 30, tzinfo=UTC)) == 0
    async with cron_db() as db:
        assert not list(
            (
                await db.scalars(
                    select(DurableChatRun).where(DurableChatRun.owner_key == owner_key)
                )
            ).all()
        )


@pytest.mark.asyncio
async def test_downgraded_owner_fails_closed(cron_db):
    async with cron_db() as db:
        await _add_active_owner(db)
        grant = await _add_grant(db, "cron-owner-active")
        owner = await db.scalar(select(User).where(User.sub == "cron-owner-active"))
        owner.role = "viewer"
        db.add(
            AgentCron(
                schedule="* * * * *",
                prompt="дай сводку дня",
                owner_key="cron-owner-active",
                delegation_grant_id=grant.id,
            )
        )
        await db.commit()

    assert await agent_cron._dispatch(datetime.now(UTC)) == 0


@pytest.mark.asyncio
async def test_revoked_grant_fails_closed(cron_db):
    async with cron_db() as db:
        await _add_active_owner(db)
        grant = await _add_grant(db, "cron-owner-active")
        grant.revoked_at = datetime.now(UTC)
        db.add(
            AgentCron(
                schedule="* * * * *",
                prompt="не запускать без гранта",
                owner_key="cron-owner-active",
                delegation_grant_id=grant.id,
            )
        )
        await db.commit()

    assert await agent_cron._dispatch(datetime(2026, 6, 12, 10, 30, tzinfo=UTC)) == 0
    async with cron_db() as db:
        assert not list(
            (
                await db.scalars(
                    select(DurableChatRun).where(DurableChatRun.owner_key == "cron-owner-active")
                )
            ).all()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actions,constraints",
    [
        (["tool_catalog.create_supplier"], {"supplier_name": "test"}),
        (["agent.cron.run"], {"schedule": "* * * * *", "prompt_sha256": "0" * 64}),
    ],
)
async def test_wrong_grant_action_or_constraint_fails_closed(cron_db, actions, constraints):
    async with cron_db() as db:
        await _add_active_owner(db)
        grant = DelegationGrant(
            owner_key="cron-owner-active",
            title="wrong cron grant",
            actions=actions,
            constraints=constraints,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(grant)
        await db.flush()
        db.add(
            AgentCron(
                schedule="* * * * *",
                prompt="дай сводку дня",
                owner_key="cron-owner-active",
                delegation_grant_id=grant.id,
            )
        )
        await db.commit()

    assert await agent_cron._dispatch(datetime(2026, 6, 12, 10, 30, tzinfo=UTC)) == 0


@pytest.mark.asyncio
async def test_grant_budget_is_consumed_and_expired_grant_is_rejected(cron_db):
    async with cron_db() as db:
        await _add_active_owner(db)
        grant = await _add_grant(db, "cron-owner-active")
        grant.max_actions = 1
        db.add(
            AgentCron(
                schedule="* * * * *",
                prompt="дай сводку дня",
                owner_key="cron-owner-active",
                delegation_grant_id=grant.id,
            )
        )
        await db.commit()

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    assert await agent_cron._dispatch(now) == 1
    assert await agent_cron._dispatch(now + timedelta(minutes=1)) == 0
    async with cron_db() as db:
        grant = (await db.execute(select(DelegationGrant))).scalars().one()
        assert grant.used_actions == 1


@pytest.mark.asyncio
async def test_expired_grant_is_rejected_against_dispatch_time(cron_db):
    async with cron_db() as db:
        await _add_active_owner(db)
        grant = await _add_grant(db, "cron-owner-active")
        grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.add(
            AgentCron(
                schedule="* * * * *",
                prompt="дай сводку дня",
                owner_key="cron-owner-active",
                delegation_grant_id=grant.id,
            )
        )
        await db.commit()

    assert await agent_cron._dispatch(datetime.now(UTC)) == 0


@pytest.mark.asyncio
async def test_malformed_legacy_grant_constraints_fail_closed_without_stopping_beat(cron_db):
    async with cron_db() as db:
        await _add_active_owner(db)
        bad_grant = await _add_grant(db, "cron-owner-active")
        bad_grant.constraints = []
        good_prompt = "вторая сводка"
        good_grant = DelegationGrant(
            owner_key="cron-owner-active",
            title="good cron grant",
            actions=["agent.cron.run"],
            constraints={
                "schedule": "* * * * *",
                "prompt_sha256": hashlib.sha256(good_prompt.encode()).hexdigest(),
            },
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add_all(
            [
                good_grant,
                AgentCron(
                    schedule="* * * * *",
                    prompt="дай сводку дня",
                    owner_key="cron-owner-active",
                    delegation_grant_id=bad_grant.id,
                ),
            ]
        )
        await db.flush()
        db.add(
            AgentCron(
                schedule="* * * * *",
                prompt=good_prompt,
                owner_key="cron-owner-active",
                delegation_grant_id=good_grant.id,
            )
        )
        await db.commit()

    assert await agent_cron._dispatch(datetime.now(UTC)) == 1


def test_beat_schedule_contains_dispatcher():
    from app.tasks.celery_app import celery_app

    entry = celery_app.conf.beat_schedule.get("agent-cron-dispatch")
    assert entry and entry["task"] == "agent.cron_dispatch"
    assert entry["schedule"] == 60.0
