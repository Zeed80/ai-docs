"""REST API for Telegram integration management.

Sensitive values (bot token, chat IDs, allowed users) are stored encrypted
in Redis. The plaintext is never returned to the frontend — only masked
previews are exposed.

The bot lifecycle is managed via _BotManager so it can be started/stopped/
restarted at runtime without restarting the whole server.
"""

from __future__ import annotations

import asyncio
import logging

import redis as _redis
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

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


# ── Runtime bot manager ───────────────────────────────────────────────────────

class _BotManager:
    """Holds a single SvetaTelegramBot instance; supports hot start/stop."""

    def __init__(self) -> None:
        self._bot: object | None = None
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._running = False
        self._last_error: str = ""

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def start(self) -> str:
        """Start (or restart) the bot. Returns "" on success, error message on failure."""
        await self.stop()

        token = get_bot_token()
        if not token:
            return "Токен бота не настроен"

        try:
            from app.integrations.telegram_bot import SvetaTelegramBot
            bot = SvetaTelegramBot(token=token, allowed_user_ids=get_allowed_users())
            await bot.start_polling()
            self._bot = bot
            self._running = True
            self._last_error = ""
            logger.info("Telegram bot started via API")
            return ""
        except Exception as exc:
            self._running = False
            self._last_error = str(exc)
            logger.warning("Telegram bot failed to start: %s", exc)
            return str(exc)

    async def stop(self) -> None:
        if self._bot is not None:
            try:
                from app.integrations.telegram_bot import SvetaTelegramBot
                if isinstance(self._bot, SvetaTelegramBot):
                    await self._bot.stop()
            except Exception:
                pass
            self._bot = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._running = False

    @property
    def last_error(self) -> str:
        return self._last_error


bot_manager = _BotManager()


# ── Pydantic models ───────────────────────────────────────────────────────────

class TelegramConfigUpdate(BaseModel):
    bot_token: str | None = None
    notifications_chat_id: str | None = None
    allowed_users: str | None = None
    notifications_enabled: bool | None = None


class TelegramConfigView(BaseModel):
    configured: bool
    bot_running: bool
    notifications_enabled: bool
    has_default_chat: bool
    allowed_users_count: int
    token_masked: str
    chat_id_masked: str
    allowed_users_masked: str
    last_error: str


class NotifyRequest(BaseModel):
    chat_id: str | None = None
    text: str


class NotifyResponse(BaseModel):
    ok: bool
    detail: str = ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status", response_model=TelegramConfigView)
async def telegram_status() -> TelegramConfigView:
    from app.utils.secret_store import decrypt, mask

    token = get_bot_token()
    chat_id = get_notifications_chat_id()
    allowed_raw = settings.telegram_allowed_users or decrypt(_r_get(_KEY_ALLOWED))
    allowed_count = len({u for u in allowed_raw.split(",") if u.strip().isdigit()})

    return TelegramConfigView(
        configured=bool(token),
        bot_running=bot_manager.running,
        notifications_enabled=get_notifications_enabled(),
        has_default_chat=bool(chat_id),
        allowed_users_count=allowed_count,
        token_masked=mask(token, 4) if token else "",
        chat_id_masked=mask(chat_id, 4) if chat_id else "",
        allowed_users_masked=allowed_raw if len(allowed_raw) <= 40 else allowed_raw[:37] + "…",
        last_error=bot_manager.last_error,
    )


@router.patch("/config")
async def update_telegram_config(body: TelegramConfigUpdate) -> TelegramConfigView:
    """Save Telegram settings encrypted in Redis, then restart the bot."""
    from app.utils.secret_store import encrypt

    if body.bot_token is not None:
        _r_set(_KEY_TOKEN, encrypt(body.bot_token) if body.bot_token else "")

    if body.notifications_chat_id is not None:
        _r_set(_KEY_CHAT_ID, encrypt(body.notifications_chat_id) if body.notifications_chat_id else "")

    if body.allowed_users is not None:
        _r_set(_KEY_ALLOWED, encrypt(body.allowed_users) if body.allowed_users else "")

    if body.notifications_enabled is not None:
        _r_set(_KEY_ENABLED, "true" if body.notifications_enabled else "false")

    # Auto-restart bot with new config
    if get_bot_token():
        asyncio.create_task(bot_manager.start())

    return await telegram_status()


@router.post("/restart")
async def telegram_restart() -> TelegramConfigView:
    """(Re)start the polling bot with current config."""
    err = await bot_manager.start()
    status = await telegram_status()
    if err:
        status.last_error = err
    return status


@router.post("/stop")
async def telegram_stop() -> TelegramConfigView:
    """Stop the polling bot."""
    await bot_manager.stop()
    return await telegram_status()


@router.post("/notify")
async def telegram_notify(req: NotifyRequest) -> NotifyResponse:
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
