"""Notification service — creates in-app notifications and pushes them in real time."""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chat_bus import chat_bus
from app.db.models import Notification, NotificationType

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
    return notif
