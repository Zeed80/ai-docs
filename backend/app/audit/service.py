"""Audit logging service — append-only."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, AuditTimelineEvent


async def log_action(
    db: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    user_id: str | None = None,
    details: dict | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=user_id,
        details=details,
        ip_address=ip_address,
    )
    db.add(entry)
    await db.flush()
    return entry


async def add_timeline_event(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    event_type: str,
    summary: str,
    actor: str | None = None,
    details: dict | None = None,
) -> AuditTimelineEvent:
    event = AuditTimelineEvent(
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        actor=actor,
        summary=summary,
        details=details,
    )
    db.add(event)
    await db.flush()
    return event
