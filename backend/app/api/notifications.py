"""Notifications API — in-app notification inbox + real-time WS push."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo
from app.db.models import Notification, NotificationType, UserNotificationPref
from app.db.session import get_db
from app.domain.proactive_feedback import record_proactive_feedback

router = APIRouter()
logger = structlog.get_logger()


class NotificationOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    type: str
    title: str
    body: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    action_url: str | None
    is_read: bool
    created_at: str


class NotificationListResponse(BaseModel):
    items: list[NotificationOut]
    total: int


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    unread: bool | None = Query(default=None),
    limit: int = Query(default=50, le=100),
    cursor: uuid.UUID | None = Query(default=None),
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationListResponse:
    stmt = (
        select(Notification)
        .where(Notification.user_sub == user.sub)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    if unread is True:
        stmt = stmt.where(Notification.is_read == False)  # noqa: E712
    elif unread is False:
        stmt = stmt.where(Notification.is_read == True)  # noqa: E712

    if cursor:
        ref = await db.execute(
            select(Notification.created_at).where(Notification.id == cursor)
        )
        ref_ts = ref.scalar_one_or_none()
        if ref_ts:
            stmt = stmt.where(Notification.created_at < ref_ts)

    total_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(total_stmt)).scalar_one()

    result = await db.execute(stmt)
    notifs = result.scalars().all()
    items = [
        NotificationOut(
            id=n.id,
            type=n.type.value,
            title=n.title,
            body=n.body,
            entity_type=n.entity_type,
            entity_id=n.entity_id,
            action_url=n.action_url,
            is_read=n.is_read,
            created_at=n.created_at.isoformat(),
        )
        for n in notifs
    ]
    return NotificationListResponse(items=items, total=total)


# ── Preferences (Ф0.8) ─────────────────────────────────────────────────────


class NotificationPrefOut(BaseModel):
    type: str
    in_app: bool = True
    push: bool = True
    private_preview: bool = False


class NotificationPrefUpdate(BaseModel):
    type: str
    in_app: bool | None = None
    push: bool | None = None
    private_preview: bool | None = None


class DeliverySettings(BaseModel):
    """Когда уведомлять и присылать ли сводкой — отдельно от «о чём».

    Часы местные: без зоны «не беспокоить с 22 до 8» означало 22:00 сервера, а
    для распределённой команды это чужая ночь.
    """

    quiet_from_hour: int | None = Field(None, ge=0, le=23)
    quiet_to_hour: int | None = Field(None, ge=0, le=23)
    digest_enabled: bool = False
    digest_hour: int = Field(9, ge=0, le=23)
    timezone: str | None = Field(None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        """Только реальная IANA-зона: строка «UTC+3» или опечатка молча
        превратилась бы в «считаем по серверу», и человек этого бы не заметил.
        """
        if not value:
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(f"Неизвестный часовой пояс: {value}") from exc
        return value


@router.get("/delivery", response_model=DeliverySettings)
async def get_delivery_settings(
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.db.models import UserNotificationSettings

    row = await db.get(UserNotificationSettings, user.sub)
    if row is None:
        return DeliverySettings()
    return DeliverySettings(
        quiet_from_hour=row.quiet_from_hour,
        quiet_to_hour=row.quiet_to_hour,
        digest_enabled=row.digest_enabled,
        digest_hour=row.digest_hour,
        timezone=row.timezone,
    )


@router.put("/delivery", response_model=DeliverySettings)
async def update_delivery_settings(
    payload: DeliverySettings,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Тихие часы и ежедневная сводка.

    Категорий было достаточно, чтобы решить, о чём уведомлять, и нечем было
    сказать «не ночью» и «одним письмом утром» — а именно из-за этого поток
    выключают целиком.
    """
    from app.db.models import UserNotificationSettings

    row = await db.get(UserNotificationSettings, user.sub)
    if row is None:
        row = UserNotificationSettings(user_sub=user.sub)
        db.add(row)
    row.quiet_from_hour = payload.quiet_from_hour
    row.quiet_to_hour = payload.quiet_to_hour
    row.digest_enabled = payload.digest_enabled
    row.digest_hour = payload.digest_hour
    row.timezone = payload.timezone
    await db.commit()
    return payload


@router.get("/preferences", response_model=list[NotificationPrefOut])
async def list_preferences(
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-type notification settings for the caller.

    Until Ф0.8 these lived only in the browser's localStorage, so the server
    kept pushing categories the user had switched off — and there was no way at
    all to keep a private mailbox's sender/subject off a phone lock screen.
    Every known type is returned, with defaults for the ones never customised.
    """
    rows = {
        r.type: r
        for r in (
            await db.execute(
                select(UserNotificationPref).where(UserNotificationPref.user_sub == user.sub)
            )
        ).scalars().all()
    }
    out = []
    for t in NotificationType:
        row = rows.get(t.value)
        out.append(
            NotificationPrefOut(
                type=t.value,
                in_app=row.in_app if row else True,
                push=row.push if row else True,
                private_preview=row.private_preview if row else False,
            )
        )
    return out


@router.put("/preferences", response_model=NotificationPrefOut)
async def update_preference(
    payload: NotificationPrefUpdate,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set one category's settings for the caller."""
    if payload.type not in {t.value for t in NotificationType}:
        raise HTTPException(422, f"Неизвестный тип уведомления: {payload.type}")
    row = (
        await db.execute(
            select(UserNotificationPref).where(
                UserNotificationPref.user_sub == user.sub,
                UserNotificationPref.type == payload.type,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserNotificationPref(user_sub=user.sub, type=payload.type)
        db.add(row)
    for field in ("in_app", "push", "private_preview"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return NotificationPrefOut(
        type=row.type, in_app=row.in_app, push=row.push, private_preview=row.private_preview
    )


@router.get("/unread-count")
async def unread_count(
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(func.count()).where(
            Notification.user_sub == user.sub,
            Notification.is_read == False,  # noqa: E712
        )
    )
    return {"count": result.scalar() or 0}


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_sub == user.sub,
        )
    )
    notif = result.scalar_one_or_none()
    if notif:
        notif.is_read = True
        await db.commit()
    return {"status": "ok"}


class NotificationFeedbackRequest(BaseModel):
    action: Literal["accepted", "dismissed", "snoozed"]
    # Only meaningful (and required in practice) for action="snoozed"; a
    # missing value there falls back to 60 minutes.
    snooze_minutes: int | None = None


class NotificationFeedbackResponse(BaseModel):
    status: str
    # False for notifications with no source_task (approvals, mentions,
    # broadcast-style proactive alerts — see Notification.source_task) — the
    # notification is still marked read, there's just no beat task to
    # calibrate this reaction against.
    calibrated: bool


@router.post("/{notification_id}/feedback", response_model=NotificationFeedbackResponse)
async def submit_notification_feedback(
    notification_id: uuid.UUID,
    payload: NotificationFeedbackRequest,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationFeedbackResponse:
    """Record accept/dismiss/snooze on a notification (AGENT_AUTONOMY_ROADMAP.md Ф0.B).

    Calibrates whether the proactive task that created it is actually wanted —
    see app.domain.proactive_feedback. Always marks the notification read: a
    reaction means the user has seen it, whether or not it's calibratable.
    """
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_sub == user.sub,
        )
    )
    notif = result.scalar_one_or_none()
    if notif is None:
        raise HTTPException(status_code=404, detail="Notification not found")

    notif.is_read = True

    if notif.source_task is None:
        await db.commit()
        return NotificationFeedbackResponse(status="ok", calibrated=False)

    snoozed_until = None
    if payload.action == "snoozed":
        snoozed_until = datetime.now(timezone.utc) + timedelta(
            minutes=payload.snooze_minutes or 60
        )

    await record_proactive_feedback(
        db,
        beat_task_name=notif.source_task,
        user_sub=user.sub,
        action=payload.action,
        notification_id=notif.id,
        entity_type=notif.entity_type,
        entity_id=notif.entity_id,
        snoozed_until=snoozed_until,
    )
    await db.commit()
    return NotificationFeedbackResponse(status="ok", calibrated=True)


@router.post("/read-all")
async def mark_all_read(
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Notification).where(
            Notification.user_sub == user.sub,
            Notification.is_read == False,  # noqa: E712
        )
    )
    notifs = result.scalars().all()
    for n in notifs:
        n.is_read = True
    await db.commit()
    return {"status": "ok", "marked": len(notifs)}


@router.websocket("/ws")
async def notifications_ws(websocket: WebSocket) -> None:
    """Real-time notification push for the current user."""
    await websocket.accept()

    token = websocket.cookies.get("access_token")
    if not token:
        await websocket.close(code=4001)
        return

    try:
        from app.auth.jwt import _verify_token
        from app.config import settings
        if settings.auth_enabled:
            user_info = await _verify_token(token)
            user_sub = user_info.sub
        else:
            user_sub = "dev-user"
    except Exception:
        await websocket.close(code=4001)
        return

    from app.core.chat_bus import chat_bus

    sid = None
    try:
        async def on_event(event: dict) -> None:
            try:
                await websocket.send_text(json.dumps(event))
            except Exception:
                pass

        sid = chat_bus.subscribe(on_event, user_sub=user_sub)

        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        if sid:
            chat_bus.unsubscribe(sid, user_sub=user_sub)
        logger.debug("notifications_ws_closed", user=user_sub)
