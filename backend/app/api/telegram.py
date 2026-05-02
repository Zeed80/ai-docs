"""REST API for Telegram integration management."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings

router = APIRouter()


class NotifyRequest(BaseModel):
    chat_id: str | None = None  # overrides settings.telegram_notifications_chat_id
    text: str


class NotifyResponse(BaseModel):
    ok: bool
    detail: str = ""


@router.get("/status")
async def telegram_status() -> dict:
    """Return whether the Telegram bot is configured and active."""
    configured = bool(settings.telegram_bot_token)
    return {
        "configured": configured,
        "notifications_enabled": settings.telegram_notifications_enabled,
        "has_default_chat": bool(settings.telegram_notifications_chat_id),
        "allowed_users_count": len(
            [u for u in settings.telegram_allowed_users.split(",") if u.strip().isdigit()]
        ) if settings.telegram_allowed_users else 0,
    }


@router.post("/notify")
async def telegram_notify(req: NotifyRequest) -> NotifyResponse:
    """Send a plain text notification to the configured chat."""
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="Telegram bot token not configured")

    chat_id = req.chat_id or settings.telegram_notifications_chat_id
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id not provided and no default configured")

    try:
        from app.integrations.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier(token=settings.telegram_bot_token, chat_id=chat_id)
        await notifier.notify_text(req.text)
        return NotifyResponse(ok=True)
    except Exception as exc:
        return NotifyResponse(ok=False, detail=str(exc))


@router.post("/test")
async def telegram_test() -> NotifyResponse:
    """Send a test message to the default chat."""
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="Telegram bot token not configured")
    if not settings.telegram_notifications_chat_id:
        raise HTTPException(status_code=400, detail="telegram_notifications_chat_id not configured")

    try:
        from app.integrations.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier(
            token=settings.telegram_bot_token,
            chat_id=settings.telegram_notifications_chat_id,
        )
        await notifier.notify_text("Тест: Света на связи ✅")
        return NotifyResponse(ok=True)
    except Exception as exc:
        return NotifyResponse(ok=False, detail=str(exc))
