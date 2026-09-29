"""E13 producer and E14 delivery boundaries for transactional outbox."""

import asyncio
import importlib.util
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.agent_runtime_models import AgentChannelIdentity, AgentOutbox
from app.db.models import WorkEvent, WorkOrder
from app.domain.agent_outbox import (
    AgentOutboxRequest,
    OutboxConflictError,
    OutboxDeliveryAdapter,
    OutboxDeliveryTimeout,
    OutboxRecipientUnavailable,
    OutboxValidationError,
    claim_due_outbox,
    deliver_due_outbox,
    deliver_outbox_claim,
    produce_agent_outbox,
)
from app.domain.work_orders import utcnow


async def _owned_destination(factory, *, owner_key="outbox-owner"):
    async with factory() as db:
        async with db.begin():
            order = WorkOrder(owner_key=owner_key, objective="Persist an outbox event")
            destination = AgentChannelIdentity(
                owner_key=owner_key, channel="telegram", external_id=uuid.uuid4().hex
            )
            db.add_all([order, destination])
            await db.flush()
            return order.id, destination.id


def _request(order_id, destination_id, *, owner_key="outbox-owner", **changes):
    values = {
        "work_order_id": order_id,
        "owner_key": owner_key,
        "destination_binding_id": destination_id,
        "event_type": "chat.reply_ready",
        "payload": {"resource_type": "work_order", "resource_id": str(order_id)},
        "dedup_key": "chat-reply:1",
    }
    values.update(changes)
    return AgentOutboxRequest(**values)


async def _persist_outbox(factory, *, channel="internal", **changes):
    owner_key = "outbox-delivery-" + uuid.uuid4().hex
    order_id, destination_id = await _owned_destination(factory, owner_key=owner_key)
    async with factory() as db:
        async with db.begin():
            destination = await db.get(AgentChannelIdentity, destination_id)
            destination.channel = channel
            outbox = await produce_agent_outbox(
                db, request=_request(order_id, destination_id, owner_key=owner_key, **changes)
            )
            outbox.next_attempt_at = utcnow() - timedelta(minutes=1)
        return outbox.id, destination_id


async def _claim_outbox(factory, outbox_id):
    """Select one synthetic row despite deliberately retained rows in this suite."""
    async with factory() as db:
        row = await db.get(AgentOutbox, outbox_id)
        row.next_attempt_at = utcnow() - timedelta(days=365)
        await db.commit()
    async with factory() as db:
        async with db.begin():
            claim = (await claim_due_outbox(db, batch_size=1))[0]
    assert claim.id == outbox_id
    return claim


class _InternalFake(OutboxDeliveryAdapter):
    is_internal = True
    supports_idempotency = True

    def __init__(self, *, crash_after_send=False):
        self.crash_after_send = crash_after_send
        self.effects: set[str] = set()
        self.calls = 0

    async def send(self, *, outbox, destination, resource, idempotency_key):
        self.calls += 1
        self.effects.add(idempotency_key or str(outbox.id))
        if self.crash_after_send:
            raise KeyboardInterrupt("worker killed after recipient effect")


class _ExternalTimeoutFake(OutboxDeliveryAdapter):
    async def send(self, **kwargs):
        raise OutboxDeliveryTimeout()


class _ExternalCrashAfterSendFake(OutboxDeliveryAdapter):
    def __init__(self):
        self.calls = 0
        self.effects: set[str] = set()

    async def send(self, *, outbox, **kwargs):
        self.calls += 1
        self.effects.add(str(outbox.id))
        raise KeyboardInterrupt("worker killed after non-idempotent recipient effect")


class _ExternalAmbiguousFailureFake(OutboxDeliveryAdapter):
    def __init__(self):
        self.calls = 0

    async def send(self, **kwargs):
        self.calls += 1
        raise ConnectionError("transport dropped after possible send")


class _UnsafeInternalFake(_ExternalAmbiguousFailureFake):
    is_internal = True


class _UnavailableFake(OutboxDeliveryAdapter):
    is_internal = True

    def __init__(self):
        self.calls = 0

    async def send(self, **kwargs):
        self.calls += 1
        raise OutboxRecipientUnavailable()


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
        with pytest.raises(OutboxValidationError, match="Payload version"):
            await produce_agent_outbox(
                db,
                request=_request(
                    order_id, destination_id, dedup_key="bool-version", payload_version=True
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


@pytest.mark.asyncio
async def test_two_workers_claim_distinct_bounded_batches(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    created_ids = []
    for key in ("one", "two", "three"):
        outbox_id, _ = await _persist_outbox(factory, dedup_key=key)
        created_ids.append(outbox_id)
    async with factory() as db:
        for outbox_id in created_ids:
            row = await db.get(AgentOutbox, outbox_id)
            row.next_attempt_at = utcnow() - timedelta(minutes=1)
        await db.commit()

    async def claim_one():
        async with factory() as db:
            async with db.begin():
                return await claim_due_outbox(db, batch_size=2)

    first, second = await asyncio.gather(claim_one(), claim_one())
    claimed = [claim.id for claim in first + second]
    assert len(set(claimed)) == len(claimed)
    assert set(created_ids).issubset(set(claimed))


@pytest.mark.asyncio
async def test_internal_duplicate_after_kill_is_recipient_idempotent(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    outbox_id, _ = await _persist_outbox(factory, dedup_key="kill-after-send")
    async with factory() as db:
        async with db.begin():
            first = (await claim_due_outbox(db))[0]
    fake = _InternalFake(crash_after_send=True)
    with pytest.raises(KeyboardInterrupt):
        await deliver_outbox_claim(factory, claim=first, adapters={"internal": fake})

    async with factory() as db:
        row = await db.get(AgentOutbox, outbox_id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    async with factory() as db:
        async with db.begin():
            second = (await claim_due_outbox(db))[0]
    fake.crash_after_send = False
    assert await deliver_outbox_claim(factory, claim=second, adapters={"internal": fake}) == "sent"
    assert fake.calls == 2
    assert len(fake.effects) == 1


@pytest.mark.asyncio
async def test_stale_lease_cannot_complete_reclaimed_delivery(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    outbox_id, _ = await _persist_outbox(factory, dedup_key="fence")
    async with factory() as db:
        async with db.begin():
            old_claim = (await claim_due_outbox(db))[0]
    async with factory() as db:
        row = await db.get(AgentOutbox, outbox_id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    async with factory() as db:
        async with db.begin():
            new_claim = (await claim_due_outbox(db))[0]
    fake = _InternalFake()
    assert (
        await deliver_outbox_claim(factory, claim=old_claim, adapters={"internal": fake}) == "stale"
    )
    assert (
        await deliver_outbox_claim(factory, claim=new_claim, adapters={"internal": fake}) == "sent"
    )


@pytest.mark.asyncio
async def test_external_timeout_is_unknown_not_retried(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    outbox_id, _ = await _persist_outbox(factory, channel="external", dedup_key="timeout")
    async with factory() as db:
        async with db.begin():
            claim = (await claim_due_outbox(db))[0]
    assert (
        await deliver_outbox_claim(
            factory, claim=claim, adapters={"external": _ExternalTimeoutFake()}
        )
        == "unknown"
    )
    async with factory() as db:
        row = await db.get(AgentOutbox, outbox_id)
        assert row.delivery_state == "unknown"
        assert row.error_code == "external_delivery_unknown"


@pytest.mark.asyncio
async def test_non_idempotent_external_kill_before_ack_becomes_unknown_without_second_send(
    test_engine,
):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    outbox_id, _ = await _persist_outbox(factory, channel="external", dedup_key="external-kill")
    async with factory() as db:
        async with db.begin():
            claim = (await claim_due_outbox(db))[0]
    fake = _ExternalCrashAfterSendFake()
    with pytest.raises(KeyboardInterrupt):
        await deliver_outbox_claim(factory, claim=claim, adapters={"external": fake})
    async with factory() as db:
        row = await db.get(AgentOutbox, outbox_id)
        assert row.delivery_state == "sending"
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    async with factory() as db:
        async with db.begin():
            assert await claim_due_outbox(db) == []
    async with factory() as db:
        row = await db.get(AgentOutbox, outbox_id)
        assert row.delivery_state == "unknown"
        assert row.error_code == "external_delivery_unknown"
    assert fake.calls == 1
    assert len(fake.effects) == 1


@pytest.mark.asyncio
async def test_empty_adapter_registry_sweeps_expired_sending_without_claiming_pending(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    sending_id, _ = await _persist_outbox(factory, channel="external", dedup_key="empty-sweep")
    pending_id, _ = await _persist_outbox(factory, channel="external", dedup_key="leave-pending")
    async with factory() as db:
        async with db.begin():
            claim = (await claim_due_outbox(db, batch_size=1))[0]
    # The first helper row is due earlier than the second, so this is the one
    # moved through the durable non-idempotent boundary.
    assert claim.id == sending_id
    async with factory() as db:
        row = await db.get(AgentOutbox, sending_id)
        row.delivery_state = "sending"
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert await deliver_due_outbox(factory, adapters={}) == []
    async with factory() as db:
        sending = await db.get(AgentOutbox, sending_id)
        pending = await db.get(AgentOutbox, pending_id)
        assert sending.delivery_state == "unknown"
        assert pending.delivery_state == "pending"


@pytest.mark.asyncio
async def test_ambiguous_non_idempotent_failure_and_unsafe_internal_are_not_retried(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    for channel, fake_type, key in (
        ("external", _ExternalAmbiguousFailureFake, "external-ambiguous"),
        ("internal", _UnsafeInternalFake, "unsafe-internal"),
    ):
        outbox_id, _ = await _persist_outbox(factory, channel=channel, dedup_key=key)
        async with factory() as db:
            row = await db.get(AgentOutbox, outbox_id)
            row.next_attempt_at = utcnow() - timedelta(days=365)
            await db.commit()
        async with factory() as db:
            async with db.begin():
                claim = (await claim_due_outbox(db, batch_size=1))[0]
        assert claim.id == outbox_id
        fake = fake_type()
        assert (
            await deliver_outbox_claim(factory, claim=claim, adapters={channel: fake}) == "unknown"
        )
        assert fake.calls == 1
        async with factory() as db:
            row = await db.get(AgentOutbox, outbox_id)
            assert row.delivery_state == "unknown"
            assert row.error_code == "external_delivery_unknown"


@pytest.mark.asyncio
async def test_unavailable_or_revoked_recipient_dead_letters_without_send(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    outbox_id, binding_id = await _persist_outbox(factory, dedup_key="revoked")
    async with factory() as db:
        async with db.begin():
            binding = await db.get(AgentChannelIdentity, binding_id)
            binding.owner_key = "new-owner"
    claim = await _claim_outbox(factory, outbox_id)
    fake = _UnavailableFake()
    assert (
        await deliver_outbox_claim(factory, claim=claim, adapters={"internal": fake})
        == "dead_letter"
    )
    assert fake.calls == 0
    async with factory() as db:
        row = await db.get(AgentOutbox, outbox_id)
        assert row.delivery_state == "dead_letter"
        assert row.error_code == "recipient_binding_revoked"


@pytest.mark.asyncio
async def test_unavailable_recipient_dead_letters_after_revalidation(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    outbox_id, _ = await _persist_outbox(factory, dedup_key="unavailable")
    claim = await _claim_outbox(factory, outbox_id)
    fake = _UnavailableFake()
    assert (
        await deliver_outbox_claim(factory, claim=claim, adapters={"internal": fake})
        == "dead_letter"
    )
    assert fake.calls == 1
    async with factory() as db:
        row = await db.get(AgentOutbox, outbox_id)
        assert row.error_code == "recipient_unavailable"
