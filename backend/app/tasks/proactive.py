"""Celery periodic tasks — proactive agent: reminders and alerts."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import structlog

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

NIL_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@celery_app.task(name="proactive.check_due_dates")
def check_due_dates() -> dict:
    """Create reminders for invoices with due dates approaching in the next 3 days."""
    return asyncio.get_event_loop().run_until_complete(_check_due_dates())


@celery_app.task(name="proactive.alert_critical_anomalies")
def alert_critical_anomalies() -> dict:
    """Publish notifications for critical unresolved anomalies older than 1 hour."""
    return asyncio.get_event_loop().run_until_complete(_alert_critical_anomalies())


async def _check_due_dates() -> dict:
    from sqlalchemy import select, and_

    from app.db.models import Invoice, Reminder, InvoiceStatus
    from app.db.session import _get_session_factory

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
            # Check if a reminder already exists for this invoice
            existing = await db.execute(
                select(Reminder).where(
                    Reminder.entity_type == "invoice",
                    Reminder.entity_id == inv.id,
                    Reminder.is_sent == False,  # noqa: E712
                )
            )
            if existing.scalar_one_or_none():
                continue

            remind_at = inv.due_date - timedelta(days=1)
            if remind_at < now:
                remind_at = now + timedelta(minutes=5)

            due_str = inv.due_date.strftime("%d.%m.%Y") if inv.due_date else "?"
            reminder = Reminder(
                entity_type="invoice",
                entity_id=inv.id,
                remind_at=remind_at,
                message=(
                    f"Счёт {inv.invoice_number or str(inv.id)[:8]} — "
                    f"срок оплаты {due_str}"
                ),
            )
            db.add(reminder)
            created += 1

        await db.commit()

    logger.info("proactive_due_date_reminders", created=created)
    return {"created": created}


async def _alert_critical_anomalies() -> dict:
    from sqlalchemy import select, and_

    from app.db.models import AnomalyCard, AnomalyStatus, AnomalySeverity
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
                await chat_bus.publish({
                    "type": "notification",
                    "level": "critical",
                    "title": "Нерешённая критическая аномалия",
                    "message": anomaly.title,
                    "entity_type": "anomaly",
                    "entity_id": str(anomaly.id),
                })
                alerted += 1
            except Exception as exc:
                logger.warning(
                    "proactive_anomaly_alert_failed",
                    anomaly_id=str(anomaly.id),
                    error=str(exc),
                )

    logger.info("proactive_anomaly_alerts_sent", alerted=alerted)
    return {"alerted": alerted}
