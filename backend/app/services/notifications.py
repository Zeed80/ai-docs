"""Notification service — creates in-app notifications and pushes them in real time."""
from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.chat_bus import chat_bus
from app.db.models import Notification, NotificationType, UserNotificationPref
from app.services import push

logger = structlog.get_logger()

# Ф0.8. Preferences were browser-only, so the server pushed everything to every
# device regardless of what the user had switched off, and a personal mailbox's
# sender+subject landed on the lock screen with no way to prevent it.
_DEFAULT_PREF = {"in_app": True, "push": True, "private_preview": False}
_PRIVATE_TITLE = "Новое уведомление"
_PRIVATE_BODY = "Откройте приложение, чтобы посмотреть"


def _pref_from_row(row) -> dict:
    if row is None:
        return {**_DEFAULT_PREF, "explicit": False}
    return {
        "in_app": row.in_app,
        "push": row.push,
        "private_preview": row.private_preview,
        "explicit": True,
    }


def _redact(pref: dict, force_private: bool, title: str, body: str) -> tuple[str, str]:
    """Push payload for this preference.

    ``force_private`` is the caller's default for inherently sensitive content
    (mail in a personal mailbox). An explicit preference row always wins, so a
    user who deliberately turned previews on keeps them.
    """
    private = pref["private_preview"] or (force_private and not pref["explicit"])
    return (_PRIVATE_TITLE, _PRIVATE_BODY) if private else (title, body)


async def get_notification_pref(db: AsyncSession, user_sub: str, type_value: str) -> dict:
    from sqlalchemy import select

    row = (
        await db.execute(
            select(UserNotificationPref).where(
                UserNotificationPref.user_sub == user_sub,
                UserNotificationPref.type == type_value,
            )
        )
    ).scalar_one_or_none()
    return _pref_from_row(row)


def get_notification_pref_sync(db: Session, user_sub: str, type_value: str) -> dict:
    from sqlalchemy import select

    row = db.execute(
        select(UserNotificationPref).where(
            UserNotificationPref.user_sub == user_sub,
            UserNotificationPref.type == type_value,
        )
    ).scalar_one_or_none()
    return _pref_from_row(row)


async def _user_timezone(db, user_sub: str) -> str | None:
    """Зона из профиля пользователя (users.timezone)."""
    from sqlalchemy import select

    from app.db.models import User

    try:
        return (
            await db.execute(select(User.timezone).where(User.sub == user_sub))
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        return None


def local_hour(timezone_name: str | None) -> int:
    """Текущий час в зоне пользователя.

    Неизвестная или битая зона — не повод уронить уведомление: тогда как
    раньше, по времени сервера.
    """
    from datetime import datetime, timezone as _tz

    now = datetime.now(_tz.utc)
    if timezone_name:
        try:
            from zoneinfo import ZoneInfo

            return now.astimezone(ZoneInfo(timezone_name)).hour
        except Exception:  # noqa: BLE001
            logger.debug("unknown_user_timezone", timezone=timezone_name)
    return now.astimezone().hour


def in_quiet_window(hour: int, start: int | None, end: int | None) -> bool:
    """Попадает ли час в окно тишины. Отдельно от БД — чтобы это можно было
    проверить, не подменяя системное время."""
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= hour < end
    # Окно через полночь: 22 → 8.
    return hour >= start or hour < end


async def _push_allowed_now(db, user_sub: str) -> bool:
    """False, когда сейчас тихие часы или человек ждёт сводку вместо потока."""
    from sqlalchemy import select

    from app.db.models import UserNotificationSettings

    try:
        row = (
            await db.execute(
                select(UserNotificationSettings).where(
                    UserNotificationSettings.user_sub == user_sub
                )
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — настройки не должны ронять уведомление
        return True
    if row is None:
        return True
    if row.digest_enabled:
        return False
    start, end = row.quiet_from_hour, row.quiet_to_hour
    if start is None or end is None:
        return True
    return not in_quiet_window(local_hour(await _user_timezone(db, user_sub)), start, end)


async def create_notification(
    db: AsyncSession,
    user_sub: str,
    type: NotificationType,
    title: str,
    body: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    action_url: str | None = None,
    source_task: str | None = None,
    private_preview: bool = False,
) -> Notification:
    """Create a Notification record and push it via WebSocket if the user is connected.

    ``private_preview`` — this content is sensitive by nature (mail in someone's
    personal mailbox), so keep sender and subject off the phone's lock screen
    unless the user explicitly said otherwise.

    ``source_task`` — the Celery task name (e.g. "proactive.check_due_dates")
    when this notification comes from a per-user proactive beat task; leave
    None for approvals/mentions/system notifications and for proactive tasks
    that broadcast org-wide instead of targeting one user (see
    Notification.source_task and app.domain.proactive_feedback). It lets the
    user's accept/dismiss/snooze reaction (POST /notifications/{id}/feedback)
    be attributed back to the task that created it.
    """
    notif = Notification(
        user_sub=user_sub,
        type=type,
        title=title,
        body=body,
        entity_type=entity_type,
        entity_id=entity_id,
        action_url=action_url,
        source_task=source_task,
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
            "source_task": source_task,
        },
    }
    await chat_bus.push_to_user(user_sub, event)

    # System push to the user's mobile devices (best-effort; never blocks the caller).
    try:
        pref = await get_notification_pref(db, user_sub, type.value)
        # Тихие часы и режим сводки: push не будит человека ночью и не сыплется
        # поштучно, если он попросил присылать одним письмом утром. In-app
        # уведомление остаётся — оно никого не будит.
        if pref["push"] and await _push_allowed_now(db, user_sub):
            push_title, push_body = _redact(pref, private_preview, title, body)
            await push.push_to_user(
                db,
                user_sub,
                push_title,
                push_body,
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
    source_task: str | None = None,
    private_preview: bool = False,
) -> Notification:
    """Synchronous notification creation for Celery tasks (sync Session).

    Persists the Notification row and dispatches a mobile push. Real-time WebSocket
    fan-out is skipped here — the in-app bell picks it up on its next REST poll/reconnect.
    See create_notification() for what ``source_task`` is for.
    """
    notif = Notification(
        user_sub=user_sub,
        type=type,
        title=title,
        body=body,
        entity_type=entity_type,
        entity_id=entity_id,
        action_url=action_url,
        source_task=source_task,
    )
    db.add(notif)
    db.flush()

    try:
        pref = get_notification_pref_sync(db, user_sub, type.value)
        if pref["push"]:
            push_title, push_body = _redact(pref, private_preview, title, body)
            push.push_to_user_sync(
                db,
                user_sub,
                push_title,
                push_body,
                action_url=action_url,
                notification_type=type.value,
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("push_dispatch_failed", user_sub=user_sub, error=str(e))

    return notif
