"""REST API for Telegram integration management.

Sensitive values (bot token, chat IDs, allowed users) are stored encrypted
in Redis. The plaintext is never returned to the frontend — only masked
previews are exposed.
"""

from __future__ import annotations

import json

import redis as _redis
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings

router = APIRouter()

# Redis keys
_KEY_TOKEN = "telegram:config:bot_token"
_KEY_CHAT_ID = "telegram:config:notifications_chat_id"
_KEY_ALLOWED = "telegram:config:allowed_users"
_KEY_ENABLED = "telegram:config:notifications_enabled"


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _redis_client() -> _redis.Redis:
    return _redis.from_url(settings.redis_url, decode_responses=True)


def _r_get(key: str) -> str:
    try:
        return _redis_client().get(key) or ""
    except Exception:
        return ""


def _r_set(key: str, value: str) -> None:
    try:
        _redis_client().set(key, value)
    except Exception:
        pass


# ── Public helpers (used by lifespan / notifier) ──────────────────────────────

def get_bot_token() -> str:
    """Return the plaintext bot token (env var takes precedence)."""
    if settings.telegram_bot_token:
        return settings.telegram_bot_token
    from app.utils.secret_store import decrypt
    return decrypt(_r_get(_KEY_TOKEN))


def get_notifications_chat_id() -> str:
    """Return the plaintext notifications chat_id."""
    if settings.telegram_notifications_chat_id:
        return settings.telegram_notifications_chat_id
    from app.utils.secret_store import decrypt
    return decrypt(_r_get(_KEY_CHAT_ID))


def get_allowed_users() -> set[int]:
    """Return the set of allowed Telegram user IDs."""
    raw = settings.telegram_allowed_users
    if not raw:
        from app.utils.secret_store import decrypt
        raw = decrypt(_r_get(_KEY_ALLOWED))
    return {int(u.strip()) for u in raw.split(",") if u.strip().isdigit()}


def get_notifications_enabled() -> bool:
    if settings.telegram_notifications_enabled:
        return True
    val = _r_get(_KEY_ENABLED)
    return val.lower() in ("1", "true", "yes")


# ── Pydantic models ───────────────────────────────────────────────────────────

class TelegramConfigUpdate(BaseModel):
    bot_token: str | None = None          # "" to clear
    notifications_chat_id: str | None = None
    allowed_users: str | None = None      # comma-separated int IDs
    notifications_enabled: bool | None = None


class TelegramConfigView(BaseModel):
    configured: bool
    notifications_enabled: bool
    has_default_chat: bool
    allowed_users_count: int
    token_masked: str        # e.g. "**************3F9A"
    chat_id_masked: str      # e.g. "**********4521"
    allowed_users_masked: str


class NotifyRequest(BaseModel):
    chat_id: str | None = None
    text: str


class NotifyResponse(BaseModel):
    ok: bool
    detail: str = ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status", response_model=TelegramConfigView)
async def telegram_status() -> TelegramConfigView:
    """Return current Telegram config status (no plaintext secrets)."""
    from app.utils.secret_store import decrypt, mask

    token = get_bot_token()
    chat_id = get_notifications_chat_id()
    allowed_raw = settings.telegram_allowed_users or decrypt(_r_get(_KEY_ALLOWED))
    allowed_count = len({u for u in allowed_raw.split(",") if u.strip().isdigit()})

    return TelegramConfigView(
        configured=bool(token),
        notifications_enabled=get_notifications_enabled(),
        has_default_chat=bool(chat_id),
        allowed_users_count=allowed_count,
        token_masked=mask(token, 4) if token else "",
        chat_id_masked=mask(chat_id, 4) if chat_id else "",
        allowed_users_masked=allowed_raw if len(allowed_raw) <= 40 else allowed_raw[:37] + "…",
    )


@router.patch("/config")
async def update_telegram_config(body: TelegramConfigUpdate) -> TelegramConfigView:
    """Save Telegram settings encrypted in Redis."""
    from app.utils.secret_store import encrypt

    if body.bot_token is not None:
        _r_set(_KEY_TOKEN, encrypt(body.bot_token) if body.bot_token else "")

    if body.notifications_chat_id is not None:
        _r_set(_KEY_CHAT_ID, encrypt(body.notifications_chat_id) if body.notifications_chat_id else "")

    if body.allowed_users is not None:
        _r_set(_KEY_ALLOWED, encrypt(body.allowed_users) if body.allowed_users else "")

    if body.notifications_enabled is not None:
        _r_set(_KEY_ENABLED, "true" if body.notifications_enabled else "false")

    return await telegram_status()


@router.post("/notify")
async def telegram_notify(req: NotifyRequest) -> NotifyResponse:
    """Send a plain text notification to the configured chat."""
    token = get_bot_token()
    if not token:
        raise HTTPException(status_code=503, detail="Telegram bot token not configured")

    chat_id = req.chat_id or get_notifications_chat_id()
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id not provided and no default configured")

    try:
        from app.integrations.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier(token=token, chat_id=chat_id)
        await notifier.notify_text(req.text)
        return NotifyResponse(ok=True)
    except Exception as exc:
        return NotifyResponse(ok=False, detail=str(exc))


@router.post("/test")
async def telegram_test() -> NotifyResponse:
    """Send a test message to the default chat."""
    token = get_bot_token()
    if not token:
        raise HTTPException(status_code=503, detail="Telegram bot token not configured")

    chat_id = get_notifications_chat_id()
    if not chat_id:
        raise HTTPException(status_code=400, detail="Notifications chat_id not configured")

    try:
        from app.integrations.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier(token=token, chat_id=chat_id)
        await notifier.notify_text("Тест: Света на связи ✅")
        return NotifyResponse(ok=True)
    except Exception as exc:
        return NotifyResponse(ok=False, detail=str(exc))
