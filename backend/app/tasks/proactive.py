"""Celery periodic tasks — proactive agent: reminders, anomaly alerts, stale approvals.

Each task:
  1. Queries the DB for actionable items.
  2. Creates a persisted Notification via services.notifications.create_notification
     (saves to DB + pushes real-time to the user's WebSocket connection).
  3. Pushes an alert to the Telegram notifications chat (if configured).
  4. Optionally generates the message body via Ollama for richer context.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog

from app.tasks.celery_app import celery_app
from app.tasks.async_runner import run_async

logger = structlog.get_logger()

NIL_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")

# ── Celery task entry points ──────────────────────────────────────────────────


@celery_app.task(name="proactive.check_due_dates")
def check_due_dates() -> dict:
    """Create reminders for invoices with due dates approaching in the next 3 days."""
    return run_async(_check_due_dates())


@celery_app.task(name="proactive.alert_critical_anomalies")
def alert_critical_anomalies() -> dict:
    """Notify on critical unresolved anomalies older than 1 hour."""
    return run_async(_alert_critical_anomalies())


@celery_app.task(name="proactive.dispatch_due_reminders")
def dispatch_due_reminders() -> dict:
    """Send notifications for reminders whose remind_at time has passed."""
    return run_async(_dispatch_due_reminders())


@celery_app.task(name="proactive.check_stale_approvals")
def check_stale_approvals() -> dict:
    """Notify assignees about approval requests that have been pending too long."""
    return run_async(_check_stale_approvals())


@celery_app.task(name="proactive.morning_briefing")
def morning_briefing() -> dict:
    """Push the secretary's prioritised daily document-flow digest."""
    return run_async(_build_morning_briefing())


@celery_app.task(name="proactive.alert_duplicate_invoices")
def alert_duplicate_invoices() -> dict:
    """Draft-first alert on freshly-ingested invoices flagged as duplicates."""
    return run_async(_alert_duplicate_invoices())


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
    """Generate a short contextual message via AIRouter; return fallback on error."""
    try:
        from app.ai.router import ai_router
        from app.ai.schemas import AIRequest, AITask, ChatMessage

        prompt = (
            "Напиши краткое (1 предложение) уведомление для сотрудника на основе контекста.\n"
            f"Контекст: {context}\n"
            "Ответ только текстом, без кавычек."
        )
        resp = await ai_router.run(
            AIRequest(
                task=AITask.ENGINEERING_REASONING,
                messages=[ChatMessage(role="user", content=prompt)],
                confidential=True,
            )
        )
        text = (resp.text or "").strip()
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


# ── Morning briefing (secretary daily digest) ─────────────────────────────────

def _format_briefing(stats: dict, *, opener: str | None = None) -> str:
    """Render a prioritised daily digest from flow stats. Pure — unit-tested.

    Groups items by urgency: 🔴 needs action now, 🟡 today, 🟢 informational.
    Returns "" when there is genuinely nothing to report.
    """
    overdue = int(stats.get("overdue_payments", 0))
    pending_approvals = int(stats.get("pending_approvals", 0))
    open_anomalies = int(stats.get("open_anomalies", 0))
    due_soon = int(stats.get("payments_due_soon", 0))
    needs_review = int(stats.get("documents_needs_review", 0))
    quarantine = int(stats.get("quarantine_count", 0))
    unread = int(stats.get("unread_emails", 0))

    red: list[str] = []
    if overdue:
        red.append(f"просроченные платежи: {overdue}")
    if pending_approvals:
        red.append(f"ждут согласования: {pending_approvals}")
    if open_anomalies:
        red.append(f"открытые аномалии: {open_anomalies}")

    yellow: list[str] = []
    if due_soon:
        yellow.append(f"оплаты в ближайшие 3 дня: {due_soon}")
    if needs_review:
        yellow.append(f"документы на проверке: {needs_review}")
    if quarantine:
        yellow.append(f"в карантине: {quarantine}")

    green: list[str] = []
    if unread:
        green.append(f"непрочитанные письма: {unread}")

    if not (red or yellow or green):
        return ""

    lines = [opener or "🗂 Доброе утро! Сводка по документообороту:"]
    if red:
        lines.append("🔴 Требует внимания: " + "; ".join(red) + ".")
    if yellow:
        lines.append("🟡 На сегодня: " + "; ".join(yellow) + ".")
    if green:
        lines.append("🟢 К сведению: " + "; ".join(green) + ".")
    return "\n".join(lines)


async def _gather_briefing_stats() -> dict:
    """Collect document-flow counts directly from the DB (Celery-safe)."""
    from sqlalchemy import and_, func, select

    from app.db.models import (
        AnomalyCard,
        AnomalyStatus,
        Approval,
        ApprovalStatus,
        Document,
        DocumentStatus,
        EmailMessage,
        PaymentSchedule,
        QuarantineEntry,
    )
    from app.db.session import _get_session_factory

    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=3)

    async def _count(db, stmt) -> int:
        return int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0)

    async with _get_session_factory()() as db:
        pending_approvals = await _count(
            db, select(Approval).where(Approval.status == ApprovalStatus.pending)
        )
        open_anomalies = await _count(
            db, select(AnomalyCard).where(AnomalyCard.status == AnomalyStatus.open)
        )
        needs_review = await _count(
            db, select(Document).where(Document.status == DocumentStatus.needs_review)
        )
        quarantine = await _count(
            db, select(QuarantineEntry).where(QuarantineEntry.decision.is_(None))
        )
        unread = await _count(
            db, select(EmailMessage).where(EmailMessage.is_inbound == True)  # noqa: E712
        )
        overdue_payments = await _count(
            db,
            select(PaymentSchedule).where(
                and_(
                    PaymentSchedule.status.in_(("scheduled", "overdue", "partial")),
                    PaymentSchedule.due_date < now,
                )
            ),
        )
        payments_due_soon = await _count(
            db,
            select(PaymentSchedule).where(
                and_(
                    PaymentSchedule.status.in_(("scheduled", "partial")),
                    PaymentSchedule.due_date >= now,
                    PaymentSchedule.due_date <= soon,
                )
            ),
        )

    return {
        "pending_approvals": pending_approvals,
        "open_anomalies": open_anomalies,
        "documents_needs_review": needs_review,
        "quarantine_count": quarantine,
        "unread_emails": unread,
        "overdue_payments": overdue_payments,
        "payments_due_soon": payments_due_soon,
    }


async def _build_morning_briefing() -> dict:
    from app.config import settings

    if not getattr(settings, "morning_briefing_enabled", True):
        return {"sent": False, "reason": "disabled"}

    stats = await _gather_briefing_stats()
    base = _format_briefing(stats)
    if not base:
        logger.info("morning_briefing_skipped_empty")
        return {"sent": False, "reason": "nothing_to_report", "stats": stats}

    # Optional: a friendlier opener via the local model (best-effort).
    opener = await _llm_enrich(
        context=f"Утренняя сводка документооборота: {stats}",
        fallback="🗂 Доброе утро! Сводка по документообороту:",
    )
    message = _format_briefing(stats, opener=opener.splitlines()[0] if opener else None)

    # In-app push (mirrors to WS + Telegram chats subscribed to the bus).
    try:
        from app.core.chat_bus import chat_bus
        await chat_bus.publish({
            "type": "proactive.briefing",
            "content": message,
            "stats": stats,
        })
    except Exception as exc:
        logger.warning("morning_briefing_bus_failed", error=str(exc))

    # Direct Telegram notification (if configured).
    notifier = await _get_notifier()
    if notifier:
        try:
            await notifier.notify_text(message)
        except Exception as exc:
            logger.warning("morning_briefing_tg_failed", error=str(exc))

    logger.info("morning_briefing_sent", stats=stats)
    return {"sent": True, "stats": stats, "message": message}


# ── Duplicate-invoice proactive alert (draft-first) ───────────────────────────

_DUP_LABELS = {
    "duplicate_hash": "точная копия уже загруженного файла",
    "duplicate_supplier_number": "тот же поставщик и номер счёта",
    "duplicate_hash_and_number": "совпадает и файл, и номер счёта",
}


def _format_duplicate_alert(
    invoice_number: str, amount: float | None, currency: str, dup_status: str
) -> str:
    """Pure, unit-tested draft-first alert text for a duplicate invoice."""
    reason = _DUP_LABELS.get(dup_status, "признаки дубликата")
    amount_str = f" на {amount:,.2f} {currency}".replace(",", " ") if amount else ""
    return (
        f"🔁 Возможный дубль счёта №{invoice_number}{amount_str}: {reason}. "
        "Проверьте — отклонить как дубль или оставить?"
    )


async def _alert_duplicate_invoices(window_days: int = 2) -> dict:
    from sqlalchemy import and_, or_, select

    from app.db.session import _get_session_factory
    from app.domain.models import Invoice

    try:
        from app.utils.redis_client import get_sync_redis
        redis = get_sync_redis()
    except Exception:
        redis = None

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)
    alerted = 0

    async with _get_session_factory()() as db:
        result = await db.execute(
            select(Invoice).where(
                and_(
                    Invoice.created_at >= window_start,
                    or_(
                        Invoice.duplicate_status == "duplicate_hash",
                        Invoice.duplicate_status == "duplicate_supplier_number",
                        Invoice.duplicate_status == "duplicate_hash_and_number",
                    ),
                )
            )
        )
        invoices = result.scalars().all()

        for inv in invoices:
            dedup_key = f"proactive:dup_alerted:{inv.id}"
            if redis is not None:
                try:
                    if redis.get(dedup_key):
                        continue
                except Exception:
                    pass

            num = inv.invoice_number or str(inv.id)[:8]
            message = _format_duplicate_alert(
                num, inv.total_amount, inv.currency or "RUB", inv.duplicate_status
            )
            try:
                from app.core.chat_bus import chat_bus
                await chat_bus.publish({
                    "type": "proactive.duplicate_invoice",
                    "content": message,
                    "invoice_id": str(inv.id),
                    "action_url": f"/invoices/{inv.id}",
                })
            except Exception as exc:
                logger.warning("dup_alert_bus_failed", invoice_id=str(inv.id), error=str(exc))

            notifier = await _get_notifier()
            if notifier:
                try:
                    await notifier.notify_text(message)
                except Exception as exc:
                    logger.warning("dup_alert_tg_failed", invoice_id=str(inv.id), error=str(exc))

            if redis is not None:
                try:
                    redis.setex(dedup_key, 30 * 24 * 3600, "1")
                except Exception:
                    pass
            alerted += 1

    logger.info("proactive_duplicate_invoices_alerted", count=alerted)
    return {"alerted": alerted}
