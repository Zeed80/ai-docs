"""E13 transactional outbox persistence; delivery is intentionally absent."""

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.agent_runtime_models import AgentChannelIdentity, AgentOutbox
from app.db.models import WorkEvent, WorkOrder
from app.domain.agent_outbox import (
    AgentOutboxRequest,
    OutboxConflictError,
    OutboxValidationError,
    produce_agent_outbox,
)


async def _owned_destination(factory):
    async with factory() as db:
        async with db.begin():
            order = WorkOrder(owner_key="outbox-owner", objective="Persist an outbox event")
            destination = AgentChannelIdentity(
                owner_key="outbox-owner", channel="telegram", external_id=uuid.uuid4().hex
            )
            db.add_all([order, destination])
            await db.flush()
            return order.id, destination.id


def _request(order_id, destination_id, **changes):
    values = {
        "work_order_id": order_id,
        "owner_key": "outbox-owner",
        "destination_binding_id": destination_id,
        "event_type": "chat.reply_ready",
        "payload": {"resource_type": "work_order", "resource_id": str(order_id)},
        "dedup_key": "chat-reply:1",
    }
    values.update(changes)
    return AgentOutboxRequest(**values)


@pytest.mark.asyncio
async def test_producer_writes_event_and_outbox_without_commit(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id, destination_id = await _owned_destination(factory)
    async with factory() as db:
        outbox = await produce_agent_outbox(db, request=_request(order_id, destination_id))
        assert outbox.delivery_state == "pending"
        assert outbox.attempts == 0
        assert outbox.lease_token is None
        assert len(outbox.request_digest) == 64
        event = await db.get(WorkEvent, outbox.work_event_id)
        assert event is not None
        assert event.work_order_id == order_id
        assert event.payload == {"outbox": True, "payload_version": 1}
        await db.rollback()

    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(AgentOutbox)) == 0
        assert await db.scalar(select(func.count()).select_from(WorkEvent)) == 0


@pytest.mark.asyncio
async def test_producer_rolls_back_domain_event_and_outbox_together(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id, destination_id = await _owned_destination(factory)
    async with factory() as db:
        await produce_agent_outbox(db, request=_request(order_id, destination_id))
        await db.rollback()

    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(WorkEvent)) == 0
        assert await db.scalar(select(func.count()).select_from(AgentOutbox)) == 0


@pytest.mark.asyncio
async def test_concurrent_duplicate_returns_one_persisted_outbox(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id, destination_id = await _owned_destination(factory)

    async def produce():
        async with factory() as db:
            async with db.begin():
                outbox = await produce_agent_outbox(db, request=_request(order_id, destination_id))
                return outbox.id

    first_id, second_id = await asyncio.gather(produce(), produce())
    assert first_id == second_id
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(AgentOutbox)) == 1
        assert await db.scalar(select(func.count()).select_from(WorkEvent)) == 1


@pytest.mark.asyncio
async def test_concurrent_changed_duplicate_conflicts_without_extra_event(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id, destination_id = await _owned_destination(factory)

    async def produce(request):
        try:
            async with factory() as db:
                async with db.begin():
                    outbox = await produce_agent_outbox(db, request=request)
                    return ("created", outbox.id)
        except OutboxConflictError:
            return ("conflict", None)

    first, second = await asyncio.gather(
        produce(_request(order_id, destination_id)),
        produce(_request(order_id, destination_id, event_type="chat.progress")),
    )
    assert {first[0], second[0]} == {"created", "conflict"}
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AgentOutbox)
                .where(AgentOutbox.work_order_id == order_id)
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkEvent)
                .where(WorkEvent.work_order_id == order_id)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_producer_rejects_foreign_destination_and_non_reference_payload(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    order_id, destination_id = await _owned_destination(factory)
    async with factory() as db:
        with pytest.raises(OutboxValidationError, match="verified for the owner"):
            await produce_agent_outbox(
                db, request=_request(order_id, uuid.uuid4(), dedup_key="foreign-binding")
            )
        with pytest.raises(OutboxValidationError, match="only a work-order reference"):
            await produce_agent_outbox(
                db,
                request=_request(
                    order_id,
                    destination_id,
                    dedup_key="sensitive",
                    payload={"access_token": "nope"},
                ),
            )
        with pytest.raises(OutboxValidationError, match="must be an object"):
            await produce_agent_outbox(
                db, request=_request(order_id, destination_id, dedup_key="list", payload=[])
            )
        with pytest.raises(OutboxValidationError, match="positive integer"):
            await produce_agent_outbox(
                db,
                request=_request(
                    order_id,
                    destination_id,
                    dedup_key="nan",
                    payload={
                        "resource_type": "work_order",
                        "resource_id": str(order_id),
                        "resource_version": float("nan"),
                    },
                ),
            )


@pytest.mark.asyncio
async def test_outbox_migration_upgrade_downgrade_upgrade(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parents[1] / "migrations/versions/20260929_0002_agent_outbox.py"
    spec = importlib.util.spec_from_file_location("agent_outbox_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "outbox_migration_" + uuid.uuid4().hex
    connection = await db_session.connection()
    await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    await connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))

    def verify(sync):
        with Operations.context(MigrationContext.configure(sync)):
            module.upgrade()
        inspector = inspect(sync)
        assert "agent_outbox" in inspector.get_table_names(schema=schema)
        unique_constraints = inspector.get_unique_constraints("agent_outbox", schema=schema)
        assert {item["name"] for item in unique_constraints} == {
            "uq_agent_outbox_destination_dedup",
            "uq_agent_outbox_work_event",
        }
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
        assert "agent_outbox" not in inspect(sync).get_table_names(schema=schema)
        with Operations.context(MigrationContext.configure(sync)):
            module.upgrade()
        assert "agent_outbox" in inspect(sync).get_table_names(schema=schema)

    await connection.run_sync(verify)
