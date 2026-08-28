"""Celery task: send email via SMTP."""
from __future__ import annotations

import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from email.header import Header

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
    from sqlalchemy import select

    from app.tasks.async_runner import run_async

    async def _run() -> dict:
        from app.db.session import _get_session_factory
        from app.db.models import DraftAction, MailboxConfig
        from app.config import settings
        from app.audit.service import log_action
        from app.utils.crypto import decrypt_password

        async with _get_session_factory()() as db:
            # Row lock: serialises concurrent send tasks for the same draft so a
            # double-dispatch cannot send the email twice.
            result = await db.execute(
                select(DraftAction)
                .where(DraftAction.id == uuid.UUID(draft_id))
                .with_for_update()
            )
            draft = result.scalar_one_or_none()
            if not draft:
                logger.error("email_draft_not_found", draft_id=draft_id)
                return {"status": "error", "reason": "draft_not_found"}

            if draft.executed or (draft.draft_data or {}).get("status") in ("sent", "sent_mock"):
                return {"status": "already_sent"}

            data = draft.draft_data or {}

            def _bare(addrs: list[str]) -> list[str]:
                return [a for _, a in getaddresses(addrs or []) if a and "@" in a]

            to_addresses: list[str] = _bare(data.get("to_addresses", []))
            cc_addresses: list[str] = _bare(data.get("cc_addresses") or [])
            subject: str = data.get("subject", "(без темы)")
            body_html: str = data.get("body_html", "")
            body_text: str = data.get("body_text") or body_html

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

            # Threading headers so the reply threads in the recipient's client.
            from app.db.models import EmailAttachment, EmailMessage
            from app.domain.email_thread import resolve_threading_headers, record_outbound_message

            parent = None
            raw_parent = data.get("in_reply_to_message_id")
            if raw_parent:
                parent = (
                    await db.execute(
                        select(EmailMessage).where(EmailMessage.id == uuid.UUID(str(raw_parent)))
                    )
                ).scalar_one_or_none()
            in_reply_to, references = resolve_threading_headers(parent)

            smtp_message_id = make_msgid(domain=(from_address.split("@")[-1] or "localhost"))

            att_rows = []
            att_ids = data.get("attachment_ids") or []
            if att_ids:
                att_rows = (
                    await db.execute(
                        select(EmailAttachment).where(
                            EmailAttachment.id.in_([uuid.UUID(str(a)) for a in att_ids])
                        )
                    )
                ).scalars().all()

            try:
                alt = MIMEMultipart("alternative")
                alt.attach(MIMEText(body_text, "plain", "utf-8"))
                if body_html:
                    alt.attach(MIMEText(body_html, "html", "utf-8"))

                if att_rows:
                    msg = MIMEMultipart("mixed")
                    msg.attach(alt)
                    from app.storage import download_file

                    for a in att_rows:
                        try:
                            payload_bytes = download_file(a.storage_path)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("email_attachment_download_failed", name=a.filename, error=str(exc))
                            continue
                        part = MIMEApplication(payload_bytes, _subtype="octet-stream")
                        part.add_header("Content-Disposition", "attachment", filename=a.filename)
                        msg.attach(part)
                else:
                    msg = alt

                msg["Subject"] = Header(subject, "utf-8")
                msg["From"] = formataddr((str(Header(from_name, "utf-8")) if from_name else "", from_address))
                msg["To"] = ", ".join(to_addresses)
                if cc_addresses:
                    msg["Cc"] = ", ".join(cc_addresses)
                msg["Date"] = formatdate(localtime=True)
                msg["Message-ID"] = smtp_message_id
                msg["MIME-Version"] = "1.0"
                if in_reply_to:
                    msg["In-Reply-To"] = in_reply_to
                if references:
                    msg["References"] = references

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

                recipients = list(to_addresses) + list(cc_addresses) + _bare(data.get("bcc_addresses") or [])
                context = ssl.create_default_context()
                if not smtp_use_tls:
                    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
                        _authenticate(server)
                        server.sendmail(from_address, recipients, msg.as_string())
                else:
                    with smtplib.SMTP(smtp_host, smtp_port) as server:
                        server.ehlo()
                        server.starttls(context=context)
                        _authenticate(server)
                        server.sendmail(from_address, recipients, msg.as_string())

                draft.executed = True
                draft.executed_at = datetime.now(timezone.utc)
                data["status"] = "sent"
                data["smtp_message_id"] = smtp_message_id
                draft.draft_data = data
                from sqlalchemy.orm.attributes import flag_modified as _fm
                _fm(draft, "draft_data")

                # Reflect the sent message into our own thread view + search.
                try:
                    await record_outbound_message(
                        db,
                        mailbox=mailbox_name or "",
                        draft_data=data,
                        smtp_message_id=smtp_message_id,
                        from_address=from_address,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("email_outbound_record_failed", draft_id=draft_id, error=str(exc))

                try:
                    from app.core.chat_bus import chat_bus

                    await chat_bus.publish({"type": "email.sent", "mailbox": mailbox_name,
                                            "to": to_addresses, "subject": subject})
                except Exception:  # noqa: BLE001
                    pass

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

    return run_async(_run())
