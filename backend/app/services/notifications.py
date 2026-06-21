"""Notification service — creates in-app notifications and pushes them in real time."""
from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.chat_bus import chat_bus
from app.db.models import Notification, NotificationType
from app.services import push

logger = structlog.get_logger()


async def create_notification(
    db: AsyncSession,
    user_sub: str,
    type: NotificationType,
    title: str,
    body: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    action_url: str | None = None,
) -> Notification:
    """Create a Notification record and push it via WebSocket if the user is connected."""
    notif = Notification(
        user_sub=user_sub,
        type=type,
        title=title,
        body=body,
        entity_type=entity_type,
        entity_id=entity_id,
        action_url=action_url,
    )
    db.add(notif)
    await db.flush()

    event = {
        "type": "notification",
        "data": {
            "id": str(notif.id),
            "type": type.value,
            "title": title,
            "body": body,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id else None,
            "action_url": action_url,
            "is_read": False,
            "created_at": notif.created_at.isoformat() if notif.created_at else None,
        },
    }
    await chat_bus.push_to_user(user_sub, event)

    # System push to the user's mobile devices (best-effort; never blocks the caller).
    try:
        await push.push_to_user(
            db,
            user_sub,
            title,
            body,
            action_url=action_url,
            notification_type=type.value,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("push_dispatch_failed", user_sub=user_sub, error=str(e))

    return notif


def create_notification_sync(
    db: Session,
    user_sub: str,
    type: NotificationType,
    title: str,
    body: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    action_url: str | None = None,
) -> Notification:
    """Synchronous notification creation for Celery tasks (sync Session).

    Persists the Notification row and dispatches a mobile push. Real-time WebSocket
    fan-out is skipped here — the in-app bell picks it up on its next REST poll/reconnect.
    """
    notif = Notification(
        user_sub=user_sub,
        type=type,
        title=title,
        body=body,
        entity_type=entity_type,
        entity_id=entity_id,
        action_url=action_url,
    )
    db.add(notif)
    db.flush()

    try:
        push.push_to_user_sync(
            db,
            user_sub,
            title,
            body,
            action_url=action_url,
            notification_type=type.value,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("push_dispatch_failed", user_sub=user_sub, error=str(e))

    return notif
