"""Notifications API — in-app notification inbox + real-time WS push."""
from __future__ import annotations

import json
import uuid

import structlog
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo
from app.db.models import Notification
from app.db.session import get_db

router = APIRouter()
logger = structlog.get_logger()


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    action_url: str | None
    is_read: bool
    created_at: str

    class Config:
        from_attributes = True


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
