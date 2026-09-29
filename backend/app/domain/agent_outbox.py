"""Transactional producer for persisted agent events; E14 owns delivery."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.agent_runtime_models import AgentChannelIdentity, AgentOutbox
from app.db.models import WorkOrder
from app.domain.work_orders import append_event, utcnow


class OutboxValidationError(ValueError):
    """The producer refused an unsafe or unbound outbox request."""


class OutboxConflictError(OutboxValidationError):
    """A dedup key was reused for a different logical notification/job."""

    status_code = 409


class OutboxDeliveryTimeout(TimeoutError):
    """The transport did not establish whether an external effect happened."""


class OutboxRecipientUnavailable(RuntimeError):
    """The adapter proved the recipient rejected the request before any send.

    Adapters must use this only for a definitive pre-send rejection (for
    example, a locally validated revoked recipient or a provider response that
    guarantees no acceptance).  A dropped connection or an ambiguous provider
    error must use a different exception and becomes ``unknown`` for a
    non-idempotent recipient.
    """


class OutboxDeliveryAdapter:
    """Small explicit boundary for delivery implementations.

    A production adapter must prove that its receiver honors the supplied
    idempotency key before setting ``supports_idempotency``.  Being internal is
    not proof by itself. E14 deliberately ships without any external adapter
    registration; tests pass fakes directly and no worker can send a message
    merely because a row appeared in the database.
    """

    is_internal = False
    supports_idempotency = False

    async def send(
        self,
        *,
        outbox: AgentOutbox,
        destination: AgentChannelIdentity,
        resource: WorkOrder,
        idempotency_key: str | None,
    ) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class OutboxClaim:
    id: uuid.UUID
    lease_token: uuid.UUID


MAX_DELIVERY_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 60


@dataclass(frozen=True)
class AgentOutboxRequest:
    """Versioned owner-bound references, never free text, context, or secrets.

    E14 must fetch presentation/body data under the owner binding at delivery
    time.  This deliberately narrow E13 shape cannot prove arbitrary text is
    safe, so arbitrary text is not accepted at all.
    """

    work_order_id: uuid.UUID
    owner_key: str
    destination_binding_id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    dedup_key: str
    payload_version: int = 1
    actor: str = "agent_outbox"


def _nonblank(value: str, *, label: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise OutboxValidationError(f"{label} must be non-empty and at most {limit} characters")


def _validate_payload(payload: Any, *, work_order_id: uuid.UUID) -> dict[str, Any]:
    """Accept only an owner-bound WorkOrder reference, never content to send."""
    if not isinstance(payload, dict):
        raise OutboxValidationError("Outbox payload must be an object")
    allowed = {"resource_type", "resource_id", "resource_version"}
    if set(payload) - allowed or not {"resource_type", "resource_id"} <= set(payload):
        raise OutboxValidationError("Outbox payload must contain only a work-order reference")
    if payload["resource_type"] != "work_order":
        raise OutboxValidationError("Outbox payload resource type must be work_order")
    if payload["resource_id"] != str(work_order_id):
        raise OutboxValidationError("Outbox payload must reference its work order")
    version = payload.get("resource_version")
    if version is not None and (
        not isinstance(version, int) or isinstance(version, bool) or version < 1
    ):
        raise OutboxValidationError("Outbox resource version must be a positive integer")
    return payload


def _request_digest(request: AgentOutboxRequest, payload: dict[str, Any]) -> str:
    canonical = {
        "work_order_id": str(request.work_order_id),
        "owner_key": request.owner_key,
        "destination_binding_id": str(request.destination_binding_id),
        "event_type": request.event_type,
        "payload": payload,
        "payload_version": request.payload_version,
        "dedup_key": request.dedup_key,
        "actor": request.actor,
    }
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _lock_key(request: AgentOutboxRequest) -> int:
    raw = f"{request.owner_key}:{request.destination_binding_id}:{request.dedup_key}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


async def produce_agent_outbox(db: AsyncSession, *, request: AgentOutboxRequest) -> AgentOutbox:
    """Persist one WorkEvent and one outbox row without committing or delivering.

    The caller owns the transaction.  PostgreSQL's transaction-scoped advisory
    lock serializes a logical delivery before event allocation, avoiding a
    duplicate orphan domain event while the database unique constraint remains
    the final deduplication authority.
    """
    _nonblank(request.owner_key, label="Owner", limit=200)
    _nonblank(request.event_type, label="Event type", limit=100)
    _nonblank(request.dedup_key, label="Dedup key", limit=300)
    _nonblank(request.actor, label="Actor", limit=200)
    if (
        not isinstance(request.payload_version, int)
        or isinstance(request.payload_version, bool)
        or not 1 <= request.payload_version <= 32767
    ):
        raise OutboxValidationError("Payload version must be a positive small integer")
    payload = _validate_payload(request.payload, work_order_id=request.work_order_id)
    try:
        encoded_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise OutboxValidationError("Outbox payload must be JSON data") from exc
    if len(encoded_payload.encode()) > 65536:
        raise OutboxValidationError("Outbox payload is too large")
    request_digest = _request_digest(request, payload)

    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(request)})
    existing = await db.scalar(
        select(AgentOutbox).where(
            AgentOutbox.owner_key == request.owner_key,
            AgentOutbox.destination_binding_id == request.destination_binding_id,
            AgentOutbox.dedup_key == request.dedup_key,
        )
    )
    if existing is not None:
        if existing.request_digest != request_digest:
            raise OutboxConflictError("Dedup key already used for different outbox request")
        return existing

    order = await db.scalar(
        select(WorkOrder)
        .where(WorkOrder.id == request.work_order_id, WorkOrder.owner_key == request.owner_key)
        .with_for_update()
    )
    if order is None:
        raise OutboxValidationError("Work order is not owned by the verified owner")
    destination = await db.scalar(
        select(AgentChannelIdentity).where(
            AgentChannelIdentity.id == request.destination_binding_id,
            AgentChannelIdentity.owner_key == request.owner_key,
            AgentChannelIdentity.is_active.is_(True),
        )
    )
    if destination is None:
        raise OutboxValidationError("Destination binding is not verified for the owner")

    event = await append_event(
        db,
        order.id,
        request.event_type,
        actor=request.actor,
        payload={"outbox": True, "payload_version": request.payload_version},
    )
    outbox = AgentOutbox(
        work_event_id=event.id,
        work_order_id=order.id,
        owner_key=request.owner_key,
        destination_binding_id=destination.id,
        event_type=request.event_type,
        payload=payload,
        payload_version=request.payload_version,
        dedup_key=request.dedup_key,
        request_digest=request_digest,
    )
    db.add(outbox)
    await db.flush()
    return outbox


def _backoff(attempts: int) -> timedelta:
    """Bound retries away from a hot loop (1, 2, 4, 8, 16 minutes)."""
    return timedelta(minutes=min(2 ** max(0, attempts - 1), 16))


async def sweep_expired_non_idempotent_sends(db: AsyncSession, *, now: Any | None = None) -> int:
    """Expose abandoned pre-send-marked deliveries without retrying them."""
    now = now or utcnow()
    result = await db.execute(
        update(AgentOutbox)
        .where(
            AgentOutbox.delivery_state == "sending",
            AgentOutbox.lease_expires_at <= now,
        )
        .values(
            delivery_state="unknown",
            error_code="external_delivery_unknown",
            lease_token=None,
            lease_expires_at=None,
        )
    )
    return result.rowcount


async def claim_due_outbox(
    db: AsyncSession,
    *,
    batch_size: int = 25,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: Any | None = None,
) -> list[OutboxClaim]:
    """Atomically lease a bounded batch; SKIP LOCKED prevents double claims."""
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    now = now or utcnow()
    # A repeatedly abandoned row is terminal rather than becoming a permanent
    # scheduler hot loop.  This update is also fenced by its expired lease.
    # ``sending`` is deliberately not reclaimable: a non-idempotent recipient
    # may already have observed the effect before its worker died.  Conserving
    # visibility as unknown is safer than silently sending a second message.
    await sweep_expired_non_idempotent_sends(db, now=now)
    await db.execute(
        update(AgentOutbox)
        .where(
            AgentOutbox.delivery_state == "leased",
            AgentOutbox.lease_expires_at <= now,
            AgentOutbox.attempts >= MAX_DELIVERY_ATTEMPTS,
        )
        .values(
            delivery_state="dead_letter",
            error_code="delivery_attempts_exhausted",
            lease_token=None,
            lease_expires_at=None,
        )
    )
    rows = (
        await db.scalars(
            select(AgentOutbox)
            .where(
                AgentOutbox.attempts < MAX_DELIVERY_ATTEMPTS,
                or_(
                    and_(
                        AgentOutbox.delivery_state == "pending",
                        AgentOutbox.next_attempt_at <= now,
                    ),
                    and_(
                        AgentOutbox.delivery_state == "leased",
                        AgentOutbox.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(AgentOutbox.next_attempt_at, AgentOutbox.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claims: list[OutboxClaim] = []
    for row in rows:
        token = uuid.uuid4()
        row.delivery_state = "leased"
        row.lease_token = token
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        row.attempts += 1
        row.error_code = None
        claims.append(OutboxClaim(id=row.id, lease_token=token))
    await db.flush()
    return claims


async def _finish_claim(
    db: AsyncSession,
    *,
    claim: OutboxClaim,
    state: str,
    error_code: str | None = None,
    next_attempt_at: Any | None = None,
    expected_state: str = "leased",
) -> bool:
    """Fenced compare-and-set: a reclaimed worker cannot overwrite its heir."""
    values: dict[str, Any] = {
        "delivery_state": state,
        "error_code": error_code,
        "lease_token": None,
        "lease_expires_at": None,
    }
    if state == "sent":
        values["delivered_at"] = utcnow()
    if next_attempt_at is not None:
        values["next_attempt_at"] = next_attempt_at
    result = await db.execute(
        update(AgentOutbox)
        .where(
            AgentOutbox.id == claim.id,
            AgentOutbox.delivery_state == expected_state,
            AgentOutbox.lease_token == claim.lease_token,
        )
        .values(**values)
    )
    return result.rowcount == 1


async def _mark_non_idempotent_send_started(db: AsyncSession, *, claim: OutboxClaim) -> bool:
    """Durably cross the unsafe-send boundary before calling the transport."""
    result = await db.execute(
        update(AgentOutbox)
        .where(
            AgentOutbox.id == claim.id,
            AgentOutbox.delivery_state == "leased",
            AgentOutbox.lease_token == claim.lease_token,
        )
        .values(delivery_state="sending")
    )
    return result.rowcount == 1


async def deliver_outbox_claim(
    session_factory: Any,
    *,
    claim: OutboxClaim,
    adapters: dict[str, OutboxDeliveryAdapter],
) -> str:
    """Deliver one lease without holding a database transaction over I/O.

    The final transition is always fenced on the token.  A process killed after
    a successful internal idempotent send can therefore be reclaimed safely;
    an external transport without recipient idempotency is never retried after
    a timeout because its outcome is intrinsically unknowable.
    """
    async with session_factory() as db:
        row = await db.scalar(
            select(AgentOutbox)
            .where(
                AgentOutbox.id == claim.id,
                AgentOutbox.delivery_state == "leased",
                AgentOutbox.lease_token == claim.lease_token,
                AgentOutbox.lease_expires_at > utcnow(),
            )
            .with_for_update()
        )
        if row is None:
            await db.rollback()
            return "stale"
        destination = await db.scalar(
            select(AgentChannelIdentity).where(
                AgentChannelIdentity.id == row.destination_binding_id,
                AgentChannelIdentity.owner_key == row.owner_key,
                AgentChannelIdentity.is_active.is_(True),
            )
        )
        resource = await db.scalar(
            select(WorkOrder).where(
                WorkOrder.id == row.work_order_id,
                WorkOrder.owner_key == row.owner_key,
            )
        )
        if destination is None or resource is None:
            await _finish_claim(
                db, claim=claim, state="dead_letter", error_code="recipient_binding_revoked"
            )
            await db.commit()
            return "dead_letter"
        adapter = adapters.get(destination.channel)
        if adapter is None:
            await _finish_claim(
                db, claim=claim, state="dead_letter", error_code="unsupported_channel"
            )
            await db.commit()
            return "dead_letter"
        non_idempotent_send = not adapter.supports_idempotency
        if non_idempotent_send and not await _mark_non_idempotent_send_started(db, claim=claim):
            await db.rollback()
            return "stale"
        # Persist the revalidation before I/O but retain no DB lock while the
        # adapter is blocked on a network call.
        await db.commit()

    # The outbox ID is durable across lease reclamation; a lease token is not.
    # Recipient-side dedup must identify the logical delivery, never its worker
    # attempt.
    idempotency_key = str(row.id) if adapter.supports_idempotency else None
    try:
        await adapter.send(
            outbox=row,
            destination=destination,
            resource=resource,
            idempotency_key=idempotency_key,
        )
    except OutboxRecipientUnavailable:
        state, error, retry_at = "dead_letter", "recipient_unavailable", None
    except OutboxDeliveryTimeout:
        if adapter.supports_idempotency:
            state, error, retry_at = (
                "pending",
                "delivery_timeout",
                utcnow() + _backoff(row.attempts),
            )
        else:
            state, error, retry_at = "unknown", "external_delivery_unknown", None
    except Exception:  # noqa: BLE001 - transport failures are persisted, not hidden
        if not adapter.supports_idempotency:
            state, error, retry_at = "unknown", "external_delivery_unknown", None
        elif row.attempts >= MAX_DELIVERY_ATTEMPTS:
            state, error, retry_at = "dead_letter", "delivery_attempts_exhausted", None
        else:
            state, error, retry_at = "pending", "delivery_failed", utcnow() + _backoff(row.attempts)
    else:
        state, error, retry_at = "sent", None, None

    async with session_factory() as db:
        changed = await _finish_claim(
            db,
            claim=claim,
            state=state,
            error_code=error,
            next_attempt_at=retry_at,
            expected_state="sending" if not adapter.supports_idempotency else "leased",
        )
        await db.commit()
    return state if changed else "stale"


async def deliver_due_outbox(
    session_factory: Any,
    *,
    adapters: dict[str, OutboxDeliveryAdapter],
    batch_size: int = 25,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> list[str]:
    """Claim and process one bounded batch, without coupling work-order execution."""
    if not adapters:
        # No channel adapter is enabled in E14.  Do not claim pending rows, but
        # make a past non-idempotent pre-send boundary visible after restart or
        # deregistration rather than leaving it leased forever.
        async with session_factory() as db:
            await sweep_expired_non_idempotent_sends(db)
            await db.commit()
        return []
    async with session_factory() as db:
        claims = await claim_due_outbox(db, batch_size=batch_size, lease_seconds=lease_seconds)
        await db.commit()
    return [
        await deliver_outbox_claim(session_factory, claim=claim, adapters=adapters)
        for claim in claims
    ]
