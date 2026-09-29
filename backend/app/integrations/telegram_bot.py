"""Telegram bot for the «Света» agent.

Incoming text is persisted through the common durable intake. The polling
process does not own an agent session or wait for model execution.

Voice messages are transcribed through Ollama Whisper before being forwarded.

Approval callbacks are reserved for E16 and never reach the legacy executor.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

try:
    from telegram import (  # noqa: F401 — Bot здесь только проба доступности
        Bot,
        Update,
    )
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        filters,
    )
    from telegram.ext import (
        MessageHandler as TelegramMessageHandler,
    )

    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = Any  # type: ignore[assignment, misc]
    Application = Any  # type: ignore[assignment, misc]
    ContextTypes = Any  # type: ignore[assignment, misc]
    filters = None  # type: ignore[assignment]
    ParseMode = None  # type: ignore[assignment]

_MDV2_RE = re.compile(r"([_*\[\]()~`>#\+\-=|{}.!\\])")


def _escape(text: str) -> str:
    return _MDV2_RE.sub(r"\\\1", str(text))


class SvetaTelegramBot:
    """Wrap python-telegram-bot while execution remains durable and detached."""

    def __init__(self, token: str, allowed_user_ids: set[int]) -> None:
        if not TELEGRAM_AVAILABLE:
            raise ImportError(
                "python-telegram-bot is not installed. "
                "Add 'python-telegram-bot>=22.6,<23' to pyproject.toml."
            )
        self._token = token
        self._allowed = allowed_user_ids
        self._app: Application = Application.builder().token(token).build()
        self._register_handlers()

    def _register_handlers(self) -> None:
        app = self._app
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("reset", self._cmd_reset))
        app.add_handler(CallbackQueryHandler(self._handle_callback))
        if filters is not None:
            app.add_handler(TelegramMessageHandler(filters.VOICE, self._handle_voice))
            app.add_handler(TelegramMessageHandler(filters.Document.ALL, self._handle_document))
            app.add_handler(TelegramMessageHandler(filters.PHOTO, self._handle_photo))
            app.add_handler(
                TelegramMessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text)
            )

    # ── Auth helpers ─────────────────────────────────────────────────────────

    def _is_allowed(self, user_id: int) -> bool:
        return user_id in self._allowed

    async def _is_private_bound_message(self, update: Update, user_id: int) -> bool:
        """Fail closed before reading private payloads from Telegram."""
        from sqlalchemy import select

        from app.db.agent_runtime_models import AgentChannelIdentity
        from app.db.models import User
        from app.db.session import _get_session_factory

        if update.effective_chat.type != "private":
            await update.message.reply_text(
                "Работа с личными данными доступна только в личном чате."
            )
            return False
        async with _get_session_factory()() as db:
            owner = await db.scalar(
                select(AgentChannelIdentity.owner_key)
                .join(User, User.sub == AgentChannelIdentity.owner_key)
                .where(
                    AgentChannelIdentity.channel == "telegram",
                    AgentChannelIdentity.external_id == str(user_id),
                    AgentChannelIdentity.is_active.is_(True),
                    User.is_active.is_(True),
                )
            )
        if owner is None:
            await update.message.reply_text(
                "Telegram не связан с учётной записью. Обратитесь к администратору."
            )
            return False
        return True

    # ── Session management ───────────────────────────────────────────────────

    # ── Handlers ─────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            await update.message.reply_text("Доступ запрещён.")
            return
        await update.message.reply_text(
            f"Привет, {user.first_name}! Я Света — ваш ИИ-помощник по документообороту.\n"
            "Отправьте сообщение или используйте /reset для сброса сессии."
        )

    async def _cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            return
        await update.message.reply_text(
            "Telegram использует сохранённые работы; локальной сессии для сброса нет."
        )

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            await update.message.reply_text("Доступ запрещён.")
            return

        text = update.message.text or ""
        await self._process_message(update, user.id, text)

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            return
        if not await self._is_private_bound_message(update, user.id):
            return

        await update.message.reply_text("🎤 Транскрибирую голосовое сообщение…")
        try:
            voice = update.message.voice
            file = await context.bot.get_file(voice.file_id)
            import os
            import tempfile

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
                    ChatMessage(
                        role="user", content="Transcribe the following audio to Russian text."
                    ),
                ],
                images=[b64],
                confidential=True,
            )
        )
        return resp.text or ""

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            await update.message.reply_text("Доступ запрещён.")
            return
        if not await self._is_private_bound_message(update, user.id):
            return

        doc = update.message.document
        file = await context.bot.get_file(doc.file_id)
        filename = doc.file_name or f"document_{doc.file_id}"
        await self._ingest_file(update, file, filename, doc.mime_type or "application/octet-stream")

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_allowed(user.id):
            await update.message.reply_text("Доступ запрещён.")
            return
        if not await self._is_private_bound_message(update, user.id):
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
        import os
        import tempfile

        await update.message.reply_text(f"📥 Получен файл: {filename}\nОбрабатываю…")
        try:
            with tempfile.NamedTemporaryFile(
                suffix=os.path.splitext(filename)[1] or ".bin", delete=False
            ) as tmp:
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
            await update.message.reply_text(f"⚠️ Не удалось обработать файл: {exc}")

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id
        if not self._is_allowed(user_id):
            return
        if query.message.chat.type != "private":
            return

        match = re.fullmatch(r"ta:([A-Za-z0-9_-]{8,32}):([ar])", query.data or "")
        if match is None:
            return
        from app.db.session import _get_session_factory
        from app.domain.telegram_approvals import (
            TelegramApprovalError,
            settle_telegram_approval_callback,
        )

        try:
            async with _get_session_factory()() as db:
                result = await settle_telegram_approval_callback(
                    db,
                    token=match.group(1),
                    approved=match.group(2) == "a",
                    telegram_user_id=user_id,
                )
        except TelegramApprovalError:
            result_text = "⚠️ Это подтверждение больше нельзя выполнить."
        else:
            icon = (
                "✅"
                if result.status == "approved"
                else "❌"
                if result.status == "rejected"
                else "⚠️"
            )
            result_text = f"{icon} {result.message}"
        await query.edit_message_text(f"{query.message.text}\n\n{result_text}", reply_markup=None)

    # ── Core dispatch ─────────────────────────────────────────────────────────

    async def _process_message(self, update: Update, user_id: int, text: str) -> None:
        from sqlalchemy import select
        from sqlalchemy import text as sql_text

        from app.db.agent_runtime_models import AgentChannelIdentity, DurableChatRun
        from app.db.models import User
        from app.db.session import _get_session_factory
        from app.domain.agent_intake import (
            AgentIntakeError,
            AgentIntakeRequest,
            VerifiedIntakeIdentity,
            submit_agent_intake,
        )
        from app.domain.agent_outbox import AgentOutboxRequest, produce_agent_outbox

        if update.effective_chat.type != "private":
            await update.message.reply_text(
                "Работа с личными данными доступна только в личном чате."
            )
            return
        async with _get_session_factory()() as db:
            binding = await db.scalar(
                select(AgentChannelIdentity)
                .join(
                    User,
                    User.sub == AgentChannelIdentity.owner_key,
                )
                .where(
                    AgentChannelIdentity.channel == "telegram",
                    AgentChannelIdentity.external_id == str(user_id),
                    AgentChannelIdentity.is_active.is_(True),
                    User.is_active.is_(True),
                )
                .with_for_update()
            )
            if binding is None:
                await update.message.reply_text(
                    "Telegram не связан с учётной записью. Обратитесь к администратору."
                )
                return
            message_date = getattr(update.message, "date", None)
            if not isinstance(message_date, datetime) or message_date.tzinfo is None:
                logger.warning("telegram update without a trusted message date was rejected")
                return
            # An unseen update can remain queued from before a rebind.  It
            # must not enter the new owner's conversation.
            if message_date.astimezone(UTC) < binding.created_at.astimezone(UTC):
                logger.warning("telegram update predates its active channel binding")
                return
            update_id = getattr(update, "update_id", None)
            if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
                logger.warning("telegram update without a stable update_id was rejected")
                return
            external_message_id = f"update:{update_id}"
            # Telegram update IDs belong to the bot's global stream. E12 also
            # namespaces by owner, so serialize the global ID here and refuse
            # to reinterpret an old delivery after an administrator rebinds
            # the same Telegram account to another owner.
            await db.execute(
                sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"telegram:{external_message_id}"},
            )
            previous_owner = await db.scalar(
                select(DurableChatRun.owner_key).where(
                    DurableChatRun.intake_channel == "telegram",
                    DurableChatRun.external_message_id == external_message_id,
                )
            )
            if previous_owner is not None and previous_owner != binding.owner_key:
                logger.warning("telegram update redelivered after owner rebind")
                return
            try:
                result = await submit_agent_intake(
                    db,
                    identity=VerifiedIntakeIdentity(binding.owner_key, "telegram"),
                    request=AgentIntakeRequest(
                        channel="telegram",
                        external_message_id=external_message_id,
                        request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"telegram:{update_id}"),
                        content=text,
                        source_binding_id=binding.id,
                    ),
                    commit=False,
                )
            except AgentIntakeError:
                await db.rollback()
                await update.message.reply_text("Сообщение не принято.")
                return
            await produce_agent_outbox(
                db,
                request=AgentOutboxRequest(
                    work_order_id=result.order.id,
                    owner_key=binding.owner_key,
                    destination_binding_id=binding.id,
                    event_type="chat.intake.accepted",
                    payload={
                        "resource_type": "work_order",
                        "resource_id": str(result.order.id),
                    },
                    dedup_key=f"telegram:{external_message_id}:accepted",
                    actor="telegram_intake",
                ),
            )
            await db.commit()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start_polling(self) -> None:
        await self._app.initialize()
        await self._app.start()
        # Backlog is safe now that Telegram's stable update_id is a durable
        # idempotency key. Dropping it would lose messages while the bot is off.
        await self._app.updater.start_polling(drop_pending_updates=False)
        logger.info("Telegram bot polling started")

    async def stop(self) -> None:
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        logger.info("Telegram bot stopped")
