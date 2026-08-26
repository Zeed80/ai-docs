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
        from app.db.models import DraftAction, MailboxConfig
        from app.config import settings
        from app.audit.service import log_action
        from app.utils.crypto import decrypt_password

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

            # Prefer the SMTP account of the mailbox this draft belongs to (the
            # one set up in /settings for procurement/accounting/personal etc.)
            # over the single global .env fallback — a reply in the
            # "procurement" thread must actually come from procurement's
            # address, not whatever noreply@ is configured server-wide.
            smtp_host: str | None = None
            smtp_port: int = 587
            smtp_user: str | None = None
            smtp_password: str | None = None
            smtp_use_tls = True
            from_address = settings.smtp_from
            from_name: str | None = None
            oauth_access_token: str | None = None

            mailbox_name = data.get("mailbox")
            if mailbox_name:
                mb = (
                    await db.execute(
                        select(MailboxConfig).where(
                            MailboxConfig.name == mailbox_name,
                            MailboxConfig.is_active == True,  # noqa: E712
                        )
                    )
                ).scalar_one_or_none()
                if mb and mb.smtp_host and mb.smtp_user and mb.auth_method == "oauth2":
                    from app.domain import oauth_mail
                    smtp_host = mb.smtp_host
                    smtp_port = mb.smtp_port or 587
                    smtp_user = mb.smtp_user
                    smtp_use_tls = mb.smtp_use_tls
                    from_address = mb.smtp_from_address or mb.smtp_user
                    from_name = mb.smtp_from_name
                    try:
                        oauth_access_token = await oauth_mail.get_valid_access_token(db, mb)
                    except Exception as exc:
                        logger.error("mailbox_oauth_token_failed", draft_id=draft_id, mailbox=mailbox_name, error=str(exc))
                        raise self.retry(exc=exc)
                elif mb and mb.smtp_host and mb.smtp_user and mb.smtp_password_encrypted:
                    smtp_host = mb.smtp_host
                    smtp_port = mb.smtp_port or 587
                    smtp_user = mb.smtp_user
                    smtp_password = decrypt_password(mb.smtp_password_encrypted)
                    smtp_use_tls = mb.smtp_use_tls
                    from_address = mb.smtp_from_address or mb.smtp_user
                    from_name = mb.smtp_from_name
                elif mb:
                    logger.warning(
                        "mailbox_smtp_not_configured_falling_back",
                        draft_id=draft_id, mailbox=mailbox_name,
                    )

            if not smtp_host:
                # No mailbox-specific SMTP resolved — fall back to the global
                # .env account (single shared "noreply@" sender).
                smtp_host = settings.smtp_host
                smtp_port = settings.smtp_port
                smtp_user = settings.smtp_user
                smtp_password = settings.smtp_password
                smtp_use_tls = settings.smtp_port != 465

            if not smtp_host:
                logger.warning("smtp_not_configured", draft_id=draft_id, mailbox=mailbox_name)
                # Mark as sent in dev/demo mode (no SMTP configured anywhere)
                draft.executed = True
                draft.executed_at = datetime.now(timezone.utc)
                data["status"] = "sent_mock"
                draft.draft_data = data
                await db.commit()
                return {"status": "sent_mock", "note": "SMTP not configured"}

            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = f"{from_name} <{from_address}>" if from_name else from_address
                msg["To"] = ", ".join(to_addresses)
                msg.attach(MIMEText(body_text, "plain", "utf-8"))
                if body_html:
                    msg.attach(MIMEText(body_html, "html", "utf-8"))

                def _authenticate(server: smtplib.SMTP) -> None:
                    if oauth_access_token:
                        from app.domain.oauth_mail import xoauth2_base64
                        code, resp = server.docmd(
                            "AUTH", "XOAUTH2 " + xoauth2_base64(smtp_user, oauth_access_token)
                        )
                        if code != 235:
                            raise smtplib.SMTPAuthenticationError(code, resp)
                    elif smtp_user:
                        server.login(smtp_user, smtp_password)

                context = ssl.create_default_context()
                if not smtp_use_tls:
                    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
                        _authenticate(server)
                        server.sendmail(from_address, to_addresses, msg.as_string())
                else:
                    with smtplib.SMTP(smtp_host, smtp_port) as server:
                        server.ehlo()
                        server.starttls(context=context)
                        _authenticate(server)
                        server.sendmail(from_address, to_addresses, msg.as_string())

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
