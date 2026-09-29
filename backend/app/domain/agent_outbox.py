"""Transactional producer for persisted agent events; E14 owns delivery."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.agent_runtime_models import AgentChannelIdentity, AgentOutbox
from app.db.models import WorkOrder
from app.domain.work_orders import append_event


class OutboxValidationError(ValueError):
    """The producer refused an unsafe or unbound outbox request."""


class OutboxConflictError(OutboxValidationError):
    """A dedup key was reused for a different logical notification/job."""

    status_code = 409


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
    if not isinstance(request.payload_version, int) or not 1 <= request.payload_version <= 32767:
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
