"""Push-notification helper for Telegram.

Sends formatted alerts and approval requests to a pre-configured chat.
Does NOT depend on the polling bot — works standalone via Bot.send_message().
"""

from __future__ import annotations

import re
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Bot = Any  # type: ignore[assignment, misc]
    InlineKeyboardButton = Any  # type: ignore[assignment, misc]
    InlineKeyboardMarkup = Any  # type: ignore[assignment, misc]
    ParseMode = None  # type: ignore[assignment]

_MDV2_RE = re.compile(r'([_*\[\]()~`>#\+\-=|{}.!\\])')


def _escape(text: str) -> str:
    """Escape text for Telegram MarkdownV2."""
    return _MDV2_RE.sub(r'\\\1', str(text))


class TelegramNotifier:
    """Sends notifications to a single Telegram chat.

    Usage:
        notifier = TelegramNotifier(token="...", chat_id="...")
        await notifier.notify_document_processed("ТТН-001", doc_id="123", status="approved")
    """

    def __init__(self, token: str, chat_id: str | int) -> None:
        if not TELEGRAM_AVAILABLE:
            raise ImportError(
                "python-telegram-bot is not installed. "
                "Add 'python-telegram-bot>=22.6,<23' to pyproject.toml."
            )
        self._bot = Bot(token=token)
        self._chat_id = chat_id

    async def notify_document_processed(
        self,
        doc_name: str,
        doc_id: str,
        status: str,
    ) -> None:
        icon = "✅" if status == "approved" else "📄"
        text = (
            f"{icon} *Документ обработан*\n"
            f"📋 {_escape(doc_name)}\n"
            f"Статус: `{_escape(status)}`\n"
            f"ID: `{_escape(doc_id)}`"
        )
        await self._send(text)

    async def notify_anomaly(
        self,
        description: str,
        severity: str = "medium",
    ) -> None:
        icon = "🔴" if severity == "high" else "🟡"
        text = (
            f"{icon} *Аномалия обнаружена*\n"
            f"{_escape(description)}\n"
            f"Уровень: `{_escape(severity)}`"
        )
        await self._send(text)

    async def notify_approval_required(
        self,
        skill: str,
        description: str,
        approval_id: str,
    ) -> None:
        """Send an approval request with inline Approve/Reject buttons."""
        text = (
            f"⏳ *Требуется подтверждение*\n"
            f"Действие: `{_escape(skill)}`\n"
            f"{_escape(description)}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"appr:approve:{approval_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"appr:reject:{approval_id}"),
            ]
        ])
        await self._send(text, reply_markup=keyboard)

    async def notify_text(self, text: str) -> None:
        """Send a plain (escaped) text notification."""
        await self._send(_escape(text))

    async def _send(self, text: str, **kwargs: Any) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2 if ParseMode else "MarkdownV2",
                **kwargs,
            )
        except Exception as exc:
            logger.warning("telegram notify failed: %s", exc)
