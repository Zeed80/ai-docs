"""Telegram bot for the «Света» agent.

Each Telegram user gets their own AgentSession. Messages are dispatched via
on_user_message(); responses stream via a Queue → editMessageText cadence
(Telegram doesn't support real streaming).

Voice messages are transcribed through Ollama Whisper before being forwarded.

Approval gate answers (inline buttons) call AgentSession.on_approval().
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

try:
    from telegram import (
        Update,
        Bot,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )
    from telegram.ext import (
        Application,
        CommandHandler,
        CallbackQueryHandler,
        MessageHandler as TelegramMessageHandler,
        ContextTypes,
        filters,
    )
    from telegram.constants import ParseMode
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = Any  # type: ignore[assignment, misc]
    Application = Any  # type: ignore[assignment, misc]
    ContextTypes = Any  # type: ignore[assignment, misc]
    filters = None  # type: ignore[assignment]
    ParseMode = None  # type: ignore[assignment]

_MDV2_RE = re.compile(r'([_*\[\]()~`>#\+\-=|{}.!\\])')


def _escape(text: str) -> str:
    return _MDV2_RE.sub(r'\\\1', str(text))


class SvetaTelegramBot:
    """Wraps python-telegram-bot Application; one AgentSession per Telegram user."""

    def __init__(self, token: str, allowed_user_ids: set[int]) -> None:
        if not TELEGRAM_AVAILABLE:
            raise ImportError(
                "python-telegram-bot is not installed. "
                "Add 'python-telegram-bot>=22.6,<23' to pyproject.toml."
            )
        self._token = token
        self._allowed = allowed_user_ids
        self._sessions: dict[int, Any] = {}  # user_id → AgentSession
        self._session_locks: dict[int, asyncio.Lock] = {}
        self._app: Application = (
            Application.builder().token(token).build()
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        app = self._app
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("reset", self._cmd_reset))
        app.add_handler(CallbackQueryHandler(self._handle_callback))
        if filters is not None:
            app.add_handler(
                TelegramMessageHandler(filters.VOICE, self._handle_voice)
            )
            app.add_handler(
                TelegramMessageHandler(filters.Document.ALL, self._handle_document)
            )
            app.add_handler(
                TelegramMessageHandler(filters.PHOTO, self._handle_photo)
            )
            app.add_handler(
                TelegramMessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text)
            )

    # ── Auth helpers ─────────────────────────────────────────────────────────

    def _is_allowed(self, user_id: int) -> bool:
        return not self._allowed or user_id in self._allowed

    # ── Session management ───────────────────────────────────────────────────

    async def _get_session(self, user_id: int) -> Any:
        """Get or create an AgentSession for this user (thread-safe)."""
        if user_id not in self._session_locks:
            self._session_locks[user_id] = asyncio.Lock()

        async with self._session_locks[user_id]:
            if user_id not in self._sessions:
                queue: asyncio.Queue[dict] = asyncio.Queue()

                async def send_fn(event: dict) -> None:
                    await queue.put(event)

                from app.ai.agent_loop import AgentSession
                session = AgentSession(send=send_fn)
                session._tg_queue = queue  # type: ignore[attr-defined]
                self._sessions[user_id] = session

        return self._sessions[user_id]

    async def _reset_session(self, user_id: int) -> None:
        async with self._session_locks.get(user_id, asyncio.Lock()):
            self._sessions.pop(user_id, None)

    # ── Handlers ─────────────────────────────────────────────────────────────

    async def _cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            await update.message.reply_text("Доступ запрещён.")
            return
        await update.message.reply_text(
            f"Привет, {user.first_name}! Я Света — ваш ИИ-помощник по документообороту.\n"
            "Отправьте сообщение или используйте /reset для сброса сессии."
        )

    async def _cmd_reset(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            return
        await self._reset_session(user.id)
        await update.message.reply_text("Сессия сброшена. Начинаем заново.")

    async def _handle_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            await update.message.reply_text("Доступ запрещён.")
            return

        text = update.message.text or ""
        await self._process_message(update, user.id, text)

    async def _handle_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            return

        await update.message.reply_text("🎤 Транскрибирую голосовое сообщение…")
        try:
            voice = update.message.voice
            file = await context.bot.get_file(voice.file_id)
            import tempfile
            import os

            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                await file.download_to_drive(tmp.name)
                tmp_path = tmp.name

            text = await self._transcribe(tmp_path)
            os.unlink(tmp_path)
        except Exception as exc:
            logger.warning("voice transcription failed: %s", exc)
            await update.message.reply_text("Не удалось распознать голосовое сообщение.")
            return

        if not text:
            await update.message.reply_text("Голосовое сообщение не содержит текста.")
            return

        await self._process_message(update, user.id, text)

    async def _transcribe(self, audio_path: str) -> str:
        """Transcribe audio via the local multimodal model (routed by AIRouter)."""
        import base64

        with open(audio_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        from app.ai.router import ai_router
        from app.ai.schemas import AIRequest, AITask, ChatMessage

        # The local OCR/vision model handles audio bytes as a multimodal input;
        # route through AIRouter so it stays local and uses the configured model.
        resp = await ai_router.run(
            AIRequest(
                task=AITask.INVOICE_OCR,
                messages=[
                    ChatMessage(role="user", content="Transcribe the following audio to Russian text."),
                ],
                images=[b64],
                confidential=True,
            )
        )
        return resp.text or ""

    async def _handle_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            await update.message.reply_text("Доступ запрещён.")
            return

        doc = update.message.document
        file = await context.bot.get_file(doc.file_id)
        filename = doc.file_name or f"document_{doc.file_id}"
        await self._ingest_file(update, file, filename, doc.mime_type or "application/octet-stream")

    async def _handle_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            await update.message.reply_text("Доступ запрещён.")
            return

        # Use the highest-resolution photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        filename = f"photo_{photo.file_id}.jpg"
        await self._ingest_file(update, file, filename, "image/jpeg")

    async def _ingest_file(
        self, update: Update, tg_file: Any, filename: str, mime_type: str
    ) -> None:
        """Download a Telegram file and POST it to the ingest pipeline."""
        import tempfile
        import os

        await update.message.reply_text(f"📥 Получен файл: {filename}\nОбрабатываю…")
        try:
            with tempfile.NamedTemporaryFile(suffix=os.path.splitext(filename)[1] or ".bin", delete=False) as tmp:
                await tg_file.download_to_drive(tmp.name)
                tmp_path = tmp.name

            try:
                import httpx
                from app.config import settings

                async with httpx.AsyncClient(timeout=120.0) as client:
                    with open(tmp_path, "rb") as f:
                        resp = await client.post(
                            f"http://localhost:{settings.app_port or 8000}/api/documents/ingest",
                            files={"file": (filename, f, mime_type)},
                            headers={"X-Internal-Task": "telegram"},
                        )
                    resp.raise_for_status()
                    data = resp.json()

                doc_id = data.get("document_id") or data.get("id", "")
                await update.message.reply_text(
                    f"✅ Файл принят в обработку.\n"
                    f"ID: `{_escape(str(doc_id)[:16])}`\n"
                    f"Статус будет обновлён автоматически.",
                    parse_mode=ParseMode.MARKDOWN_V2 if ParseMode else None,
                )
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        except Exception as exc:
            logger.warning("telegram file ingest failed: %s", exc)
            await update.message.reply_text(
                f"⚠️ Не удалось обработать файл: {exc}"
            )

    async def _handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id
        if not self._is_allowed(user_id):
            return

        data = query.data or ""
        if data.startswith("appr:"):
            _, action, approval_id = data.split(":", 2)
            approved = action == "approve"
            session = self._sessions.get(user_id)
            if session:
                await session.on_approval(approved)
            result_text = "✅ Подтверждено" if approved else "❌ Отклонено"
            await query.edit_message_text(
                f"{query.message.text}\n\n{result_text}",
                reply_markup=None,
            )

    # ── Core dispatch ─────────────────────────────────────────────────────────

    async def _process_message(
        self, update: Update, user_id: int, text: str
    ) -> None:
        session = await self._get_session(user_id)
        queue: asyncio.Queue[dict] = session._tg_queue  # type: ignore[attr-defined]

        # Drain any stale events from a previous turn
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Mirror user message to all connected web chat clients
        try:
            from app.core.chat_bus import chat_bus
            await chat_bus.publish({"type": "tg_user", "content": text, "source": "telegram"})
        except Exception:
            pass

        # Send placeholder; we'll edit it as tokens stream in
        placeholder = await update.message.reply_text("…")
        accumulated = ""
        last_edit = ""

        async def _edit_task() -> None:
            nonlocal last_edit
            while True:
                await asyncio.sleep(1.0)
                if accumulated and accumulated != last_edit:
                    try:
                        await placeholder.edit_text(
                            accumulated[:4096],
                            parse_mode=None,
                        )
                        last_edit = accumulated
                    except Exception:
                        pass

        edit_loop = asyncio.create_task(_edit_task())

        # Run agent turn concurrently with the drain loop
        agent_task = asyncio.create_task(session.on_user_message(text))

        try:
            from app.core.chat_bus import chat_bus
            while not agent_task.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue

                # Mirror every agent event to web chat in real time
                asyncio.create_task(chat_bus.publish({**event, "source": "telegram"}))

                ev_type = event.get("type", "")
                if ev_type == "token":
                    accumulated += event.get("content", "")
                elif ev_type == "text":
                    accumulated += event.get("content", "")
                elif ev_type == "approval_request":
                    skill = event.get("skill", "")
                    desc = event.get("description", "Разрешить выполнение?")
                    approval_id = event.get("approval_id", "")
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "✅ Подтвердить",
                                callback_data=f"appr:approve:{approval_id}",
                            ),
                            InlineKeyboardButton(
                                "❌ Отклонить",
                                callback_data=f"appr:reject:{approval_id}",
                            ),
                        ]
                    ])
                    await update.message.reply_text(
                        f"⏳ *Требуется подтверждение*\n"
                        f"Действие: `{_escape(skill)}`\n"
                        f"{_escape(desc)}",
                        parse_mode=ParseMode.MARKDOWN_V2 if ParseMode else None,
                        reply_markup=keyboard,
                    )
                elif ev_type == "done":
                    break
                elif ev_type == "error":
                    accumulated = "⚠️ " + event.get("content", "Ошибка агента.")
                    break
        finally:
            edit_loop.cancel()
            try:
                await agent_task
            except (asyncio.CancelledError, Exception):
                pass

        # Final edit with full answer
        if accumulated and accumulated != last_edit:
            try:
                await placeholder.edit_text(
                    accumulated[:4096],
                    parse_mode=None,
                )
            except Exception:
                pass

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start_polling(self) -> None:
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started")

    async def stop(self) -> None:
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        logger.info("Telegram bot stopped")
