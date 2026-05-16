"""Celery task: send email via SMTP."""
from __future__ import annotations

import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import structlog

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(
    name="email.send_draft",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    queue="scheduler",
)
def send_email_draft(self, draft_id: str) -> dict:
    """Send an email draft via SMTP. Called after approval gate is passed."""
    import asyncio
    from sqlalchemy import select

    async def _run() -> dict:
        from app.db.session import AsyncSessionLocal
        from app.db.models import DraftAction
        from app.config import settings
        from app.audit.service import log_action

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(DraftAction).where(DraftAction.id == uuid.UUID(draft_id)))
            draft = result.scalar_one_or_none()
            if not draft:
                logger.error("email_draft_not_found", draft_id=draft_id)
                return {"status": "error", "reason": "draft_not_found"}

            if draft.executed:
                return {"status": "already_sent"}

            data = draft.draft_data or {}
            to_addresses: list[str] = data.get("to_addresses", [])
            subject: str = data.get("subject", "(без темы)")
            body_html: str = data.get("body_html", "")
            body_text: str = data.get("body_text", body_html)

            if not to_addresses:
                logger.error("email_no_recipients", draft_id=draft_id)
                return {"status": "error", "reason": "no_recipients"}

            if not settings.smtp_host:
                logger.warning("smtp_not_configured", draft_id=draft_id)
                # Mark as sent in dev/demo mode (no SMTP configured)
                draft.executed = True
                draft.executed_at = datetime.now(timezone.utc)
                data["status"] = "sent_mock"
                draft.draft_data = data
                await db.commit()
                return {"status": "sent_mock", "note": "SMTP not configured"}

            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = settings.smtp_from
                msg["To"] = ", ".join(to_addresses)
                msg.attach(MIMEText(body_text, "plain", "utf-8"))
                if body_html:
                    msg.attach(MIMEText(body_html, "html", "utf-8"))

                context = ssl.create_default_context()
                if settings.smtp_port == 465:
                    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as server:
                        if settings.smtp_user:
                            server.login(settings.smtp_user, settings.smtp_password)
                        server.sendmail(settings.smtp_from, to_addresses, msg.as_string())
                else:
                    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                        server.ehlo()
                        server.starttls(context=context)
                        if settings.smtp_user:
                            server.login(settings.smtp_user, settings.smtp_password)
                        server.sendmail(settings.smtp_from, to_addresses, msg.as_string())

                draft.executed = True
                draft.executed_at = datetime.now(timezone.utc)
                data["status"] = "sent"
                draft.draft_data = data

                await log_action(
                    db,
                    action="email.send",
                    entity_type="email",
                    entity_id=draft.id,
                    details={"to": to_addresses, "subject": subject},
                )
                await db.commit()

                logger.info("email_sent_smtp", draft_id=draft_id, to=to_addresses)
                return {"status": "sent"}

            except smtplib.SMTPException as exc:
                logger.error("smtp_error", draft_id=draft_id, error=str(exc))
                raise self.retry(exc=exc)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()
