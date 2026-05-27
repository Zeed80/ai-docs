"""Celery periodic tasks — proactive agent: reminders, anomaly alerts, stale approvals.

Each task:
  1. Queries the DB for actionable items.
  2. Creates a persisted Notification via services.notifications.create_notification
     (saves to DB + pushes real-time to the user's WebSocket connection).
  3. Pushes an alert to the Telegram notifications chat (if configured).
  4. Optionally generates the message body via Ollama for richer context.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import structlog

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

NIL_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")

# ── Celery task entry points ──────────────────────────────────────────────────


@celery_app.task(name="proactive.check_due_dates")
def check_due_dates() -> dict:
    """Create reminders for invoices with due dates approaching in the next 3 days."""
    return asyncio.get_event_loop().run_until_complete(_check_due_dates())


@celery_app.task(name="proactive.alert_critical_anomalies")
def alert_critical_anomalies() -> dict:
    """Notify on critical unresolved anomalies older than 1 hour."""
    return asyncio.get_event_loop().run_until_complete(_alert_critical_anomalies())


@celery_app.task(name="proactive.dispatch_due_reminders")
def dispatch_due_reminders() -> dict:
    """Send notifications for reminders whose remind_at time has passed."""
    return asyncio.get_event_loop().run_until_complete(_dispatch_due_reminders())


@celery_app.task(name="proactive.check_stale_approvals")
def check_stale_approvals() -> dict:
    """Notify assignees about approval requests that have been pending too long."""
    return asyncio.get_event_loop().run_until_complete(_check_stale_approvals())


# ── Helpers ───────────────────────────────────────────────────────────────────



async def _get_notifier():
    """Return a TelegramNotifier if Telegram notifications are configured, else None."""
    try:
        from app.api.telegram import get_bot_token, get_notifications_chat_id, get_notifications_enabled
        if not get_notifications_enabled():
            return None
        token = get_bot_token()
        chat_id = get_notifications_chat_id()
        if not (token and chat_id):
            return None
        from app.integrations.telegram_notifier import TelegramNotifier
        return TelegramNotifier(token=token, chat_id=chat_id)
    except Exception:
        return None


async def _tg_notify_anomaly(title: str, anomaly_id: str) -> None:
    notifier = await _get_notifier()
    if notifier:
        try:
            await notifier.notify_critical_anomaly(title=title, anomaly_id=anomaly_id)
        except Exception as exc:
            logger.warning("tg_notify_anomaly_failed", error=str(exc))


async def _tg_notify_stale_approval(
    action_type: str, entity_label: str, hours_pending: int, approval_id: str
) -> None:
    notifier = await _get_notifier()
    if notifier:
        try:
            await notifier.notify_stale_approval(
                action_type=action_type,
                entity_label=entity_label,
                hours_pending=hours_pending,
                approval_id=approval_id,
            )
        except Exception as exc:
            logger.warning("tg_notify_stale_approval_failed", error=str(exc))


async def _llm_enrich(context: str, fallback: str) -> str:
    """Generate a short contextual message via Ollama; return fallback on error."""
    try:
        from app.config import settings
        import httpx

        prompt = (
            "Напиши краткое (1 предложение) уведомление для сотрудника на основе контекста.\n"
            f"Контекст: {context}\n"
            "Ответ только текстом, без кавычек."
        )
        from app.ai.model_resolver import get_ocr_model as _proactive_get_ocr
        _p_ocr = _proactive_get_ocr()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.ollama_url.rstrip('/')}/api/generate",
                json={"model": _p_ocr.model, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            text = resp.json().get("response", "").strip()
            return text if text else fallback
    except Exception:
        return fallback


# ── Task implementations ──────────────────────────────────────────────────────


async def _check_due_dates() -> dict:
    from sqlalchemy import select, and_

    from app.db.models import Invoice, InvoiceStatus, Notification, NotificationType, Reminder
    from app.db.session import _get_session_factory
    from app.services.notifications import create_notification

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=3)
    created = 0

    async with _get_session_factory()() as db:
        result = await db.execute(
            select(Invoice).where(
                and_(
                    Invoice.due_date != None,  # noqa: E711
                    Invoice.due_date >= now,
                    Invoice.due_date <= window_end,
                    Invoice.status == InvoiceStatus.needs_review,
                )
            )
        )
        invoices = result.scalars().all()

        for inv in invoices:
            # Skip if unsent reminder already exists
            existing = await db.execute(
                select(Reminder).where(
                    Reminder.entity_type == "invoice",
                    Reminder.entity_id == inv.id,
                    Reminder.is_sent == False,  # noqa: E712
                )
            )
            if existing.scalar_one_or_none():
                continue

            due_str = inv.due_date.strftime("%d.%m.%Y") if inv.due_date else "?"
            num = inv.invoice_number or str(inv.id)[:8]
            msg = f"Счёт {num} — срок оплаты {due_str}"

            remind_at = inv.due_date - timedelta(days=1)
            if remind_at < now:
                remind_at = now + timedelta(minutes=5)

            reminder = Reminder(
                entity_type="invoice",
                entity_id=inv.id,
                remind_at=remind_at,
                message=msg,
            )
            db.add(reminder)

            # Persist notification + real-time push for the invoice owner
            owner = getattr(inv, "created_by", None) or "system"
            # Only create DB notification for real users (not system placeholder)
            if owner != "system":
                await create_notification(
                    db,
                    user_sub=owner,
                    type=NotificationType.document_ready,
                    title="Приближается срок оплаты",
                    body=msg,
                    entity_type="invoice",
                    entity_id=inv.id,
                    action_url=f"/invoices/{inv.id}",
                )

            notifier = await _get_notifier()
            if notifier and inv.due_date:
                due_str_tg = inv.due_date.strftime("%d.%m.%Y")
                try:
                    await notifier.notify_due_date(
                        invoice_number=num,
                        due_date_str=due_str_tg,
                        doc_id=str(inv.id),
                    )
                except Exception as exc:
                    logger.warning("tg_notify_due_date_failed", error=str(exc))
            created += 1

        await db.commit()

    logger.info("proactive_due_date_reminders", created=created)
    return {"created": created}


async def _alert_critical_anomalies() -> dict:
    from sqlalchemy import select, and_

    from app.db.models import AnomalyCard, AnomalySeverity, AnomalyStatus
    from app.db.session import _get_session_factory
    from app.core.chat_bus import chat_bus

    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(hours=1)
    alerted = 0

    async with _get_session_factory()() as db:
        result = await db.execute(
            select(AnomalyCard).where(
                and_(
                    AnomalyCard.status == AnomalyStatus.open,
                    AnomalyCard.severity == AnomalySeverity.critical,
                    AnomalyCard.created_at <= stale_threshold,
                )
            ).limit(10)
        )
        anomalies = result.scalars().all()

        for anomaly in anomalies:
            try:
                fallback = f"Нерешённая критическая аномалия: {anomaly.title}"
                body = await _llm_enrich(
                    context=f"Критическая аномалия '{anomaly.title}' не решена более часа.",
                    fallback=fallback,
                )
                # Broadcast to all connected web clients (no specific user)
                await chat_bus.publish({
                    "type": "notification",
                    "level": "critical",
                    "title": "Критическая аномалия",
                    "body": body,
                    "entity_type": "anomaly",
                    "entity_id": str(anomaly.id),
                    "action_url": f"/anomalies/{anomaly.id}",
                })
                await _tg_notify_anomaly(anomaly.title, str(anomaly.id))
                alerted += 1
            except Exception as exc:
                logger.warning(
                    "proactive_anomaly_alert_failed",
                    anomaly_id=str(anomaly.id),
                    error=str(exc),
                )

    logger.info("proactive_anomaly_alerts_sent", alerted=alerted)
    return {"alerted": alerted}


async def _dispatch_due_reminders() -> dict:
    from sqlalchemy import select, and_

    from app.db.models import Notification, NotificationType, Reminder
    from app.db.session import _get_session_factory
    from app.services.notifications import create_notification

    now = datetime.now(timezone.utc)
    dispatched = 0

    async with _get_session_factory()() as db:
        result = await db.execute(
            select(Reminder).where(
                and_(
                    Reminder.is_sent == False,  # noqa: E712
                    Reminder.remind_at <= now,
                )
            ).limit(50)
        )
        reminders = result.scalars().all()

        for reminder in reminders:
            try:
                user_sub = reminder.user_id
                if user_sub and user_sub != "user":
                    await create_notification(
                        db,
                        user_sub=user_sub,
                        type=NotificationType.document_ready,
                        title="Напоминание",
                        body=reminder.message,
                        entity_type=reminder.entity_type,
                        entity_id=reminder.entity_id,
                        action_url=(
                            f"/{reminder.entity_type}s/{reminder.entity_id}"
                            if reminder.entity_type else None
                        ),
                    )
                else:
                    # No specific user — broadcast to web clients
                    from app.core.chat_bus import chat_bus
                    await chat_bus.publish({
                        "type": "notification",
                        "level": "info",
                        "title": "Напоминание",
                        "body": reminder.message,
                        "entity_type": reminder.entity_type,
                        "entity_id": str(reminder.entity_id),
                    })

                notifier = await _get_notifier()
                if notifier:
                    try:
                        await notifier.notify_text(f"⏰ {reminder.message}")
                    except Exception as exc:
                        logger.warning("tg_notify_reminder_failed", error=str(exc))
                reminder.is_sent = True
                reminder.sent_at = now
                dispatched += 1
            except Exception as exc:
                logger.warning(
                    "reminder_dispatch_failed",
                    reminder_id=str(reminder.id),
                    error=str(exc),
                )

        await db.commit()

    logger.info("reminders_dispatched", count=dispatched)
    return {"dispatched": dispatched}


async def _check_stale_approvals() -> dict:
    """Notify assignees about pending approvals older than STALE_HOURS hours."""
    from sqlalchemy import select, and_

    from app.db.models import Approval, ApprovalStatus, NotificationType
    from app.db.session import _get_session_factory
    from app.services.notifications import create_notification

    STALE_HOURS = 24
    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(hours=STALE_HOURS)
    alerted = 0

    async with _get_session_factory()() as db:
        result = await db.execute(
            select(Approval).where(
                and_(
                    Approval.status == ApprovalStatus.pending,
                    Approval.created_at <= stale_threshold,
                )
            ).limit(20)
        )
        approvals = result.scalars().all()

        for appr in approvals:
            try:
                hours_pending = int((now - appr.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600)
                entity_label = f"{appr.entity_type} {str(appr.entity_id)[:8]}"
                fallback = (
                    f"Запрос на подтверждение ({appr.action_type.value}) "
                    f"по {entity_label} ожидает {hours_pending}ч."
                )
                body = await _llm_enrich(
                    context=(
                        f"Запрос на подтверждение действия '{appr.action_type.value}' "
                        f"по {entity_label} ожидает ответа уже {hours_pending} часов."
                    ),
                    fallback=fallback,
                )

                assignee = appr.assigned_to or appr.requested_by
                if assignee and assignee not in ("sveta", "system"):
                    await create_notification(
                        db,
                        user_sub=assignee,
                        type=NotificationType.approval_assigned,
                        title="Ожидает вашего подтверждения",
                        body=body,
                        entity_type=appr.entity_type,
                        entity_id=appr.entity_id,
                        action_url=f"/approvals/{appr.id}",
                    )

                await _tg_notify_stale_approval(
                    action_type=appr.action_type.value,
                    entity_label=entity_label,
                    hours_pending=hours_pending,
                    approval_id=str(appr.id),
                )
                alerted += 1
            except Exception as exc:
                logger.warning(
                    "proactive_stale_approval_alert_failed",
                    approval_id=str(appr.id),
                    error=str(exc),
                )

        await db.commit()

    logger.info("proactive_stale_approvals_alerted", count=alerted)
    return {"alerted": alerted}
