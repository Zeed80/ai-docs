"""Ежедневная сводка вместо потока уведомлений.

Поток пингов на производстве выключают целиком — вместе с тем, ради чего его
заводили. Сводка «за сутки: 3 счёта, 1 аномалия, 2 ждут вашего решения»
приходит раз в день в выбранный час и остаётся единственным push'ем для тех,
кто включил этот режим (см. UserNotificationSettings.digest_enabled — при нём
поштучные push подавляются в app.services.notifications).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(name="notifications.daily_digest", bind=True)
def daily_digest(self) -> dict:
    """Разослать сводку тем, у кого сейчас наступил выбранный час."""
    from app.tasks.async_runner import run_async

    return run_async(_run())


async def _run() -> dict:
    from sqlalchemy import func, select

    from app.db.models import (
        AnomalyCard, AnomalyStatus, Approval, ApprovalStatus, Document,
        DocumentStatus, EmailThread, NotificationType, User, UserNotificationSettings,
    )
    from app.db.session import _get_session_factory
    from app.domain.email_access import hidden_mailbox_names
    from app.services.notifications import create_notification, local_hour

    now_utc = datetime.now(timezone.utc)
    since = now_utc - timedelta(days=1)
    sent = 0

    async with _get_session_factory()() as db:
        rows = (
            await db.execute(
                select(UserNotificationSettings).where(
                    UserNotificationSettings.digest_enabled == True  # noqa: E712
                )
            )
        ).scalars().all()

        for row in rows:
            # Час сводки — местный для получателя, а не серверный: иначе
            # «в девять утра» наступало по часовому поясу машины. Зона живёт в
            # профиле: одна на человека, общая для всего приложения.
            user_tz = (
                await db.execute(select(User.timezone).where(User.sub == row.user_sub))
            ).scalar_one_or_none()
            if row.digest_hour != local_hour(user_tz):
                continue
            # Уже отправляли в этот час — beat может сработать несколько раз.
            if row.last_digest_at and (now_utc - row.last_digest_at) < timedelta(hours=12):
                continue

            hidden = set(await hidden_mailbox_names(db, None))
            mail_q = select(func.count(EmailThread.id)).where(
                EmailThread.folder == "inbox",
                EmailThread.is_read == False,  # noqa: E712
                EmailThread.last_message_at >= since,
            )
            if hidden:
                mail_q = mail_q.where(EmailThread.mailbox.notin_(hidden))
            unread = (await db.execute(mail_q)).scalar() or 0

            docs = (
                await db.execute(
                    select(func.count(Document.id)).where(
                        Document.status == DocumentStatus.needs_review,
                        Document.created_at >= since,
                    )
                )
            ).scalar() or 0
            anomalies = (
                await db.execute(
                    select(func.count(AnomalyCard.id)).where(
                        AnomalyCard.status.in_(
                            (AnomalyStatus.open, AnomalyStatus.escalated)
                        ),
                    )
                )
            ).scalar() or 0
            waiting = (
                await db.execute(
                    select(func.count(Approval.id)).where(
                        Approval.status == ApprovalStatus.pending
                    )
                )
            ).scalar() or 0

            if not any((unread, docs, anomalies, waiting)):
                # Пустая сводка — тоже уведомление, которое никто не просил.
                row.last_digest_at = now_utc
                continue

            parts = []
            if waiting:
                parts.append(f"ждут решения: {waiting}")
            if unread:
                parts.append(f"писем: {unread}")
            if docs:
                parts.append(f"документов на проверке: {docs}")
            if anomalies:
                parts.append(f"аномалий: {anomalies}")

            await create_notification(
                db=db,
                user_sub=row.user_sub,
                type=NotificationType.system,
                title="Сводка за сутки",
                body=" · ".join(parts),
                action_url="/inbox",
            )
            row.last_digest_at = now_utc
            sent += 1

        await db.commit()

    logger.info("notification_digest_sent", count=sent)
    return {"sent": sent}
