"""Celery task: send email via SMTP."""
from __future__ import annotations

import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from email.header import Header

import re

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

            # Ф4 — "Отменить". Checked inside the row lock taken above, so the
            # cancel either lands before the send or loses cleanly; a Celery
            # revoke would race the worker instead.
            if data.get("cancelled"):
                from app.core.metrics import email_sent_total

                email_sent_total.labels(outcome="cancelled").inc()
                logger.info("email_send_cancelled_before_dispatch", draft_id=draft_id)
                return {"status": "cancelled"}

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

                # Defence in depth (Ф0.1). The API already refuses to put an
                # attachment the author cannot reach into a draft; re-check here
                # because this worker is the thing that actually puts bytes on
                # the wire, and a draft row can be written by other code paths.
                # A failed check aborts the send: quietly dropping the file
                # would deliver a mail that says "во вложении" with nothing
                # attached, which is worse than not sending at all.
                owner = data.get("created_by_sub")
                foreign = []
                for a in att_rows:
                    if a.uploaded_by_sub is not None and a.uploaded_by_sub == owner:
                        continue
                    if a.message_id is not None:
                        src = (
                            await db.execute(
                                select(EmailMessage.mailbox).where(EmailMessage.id == a.message_id)
                            )
                        ).scalar_one_or_none()
                        if src is not None and src == mailbox_name:
                            continue
                    foreign.append(a.filename)
                # Total-size guard: per-file limits do not stop ten files that
                # together exceed what the relay accepts. Refusing here (with a
                # visible status) beats an opaque SMTP rejection after retries.
                from app.db.models import MailServerConfig as _MSC

                cfg_row = (await db.execute(select(_MSC))).scalars().first()
                # Ф9 — a mailbox whose relay accepts less (or more) than the
                # company default says so on its own row; NULL = inherit.
                from app.db.models import MailboxConfig as _MBC

                box_limit = (await db.execute(
                    select(_MBC.max_attachment_mb).where(_MBC.name == mailbox_name)
                )).scalar_one_or_none()
                limit_mb = box_limit if box_limit is not None else (
                    (cfg_row.max_attachment_mb if cfg_row else 25) or 25
                )
                max_total = limit_mb * 1024 * 1024
                total_size = sum(int(a.size or 0) for a in att_rows)
                if total_size > max_total:
                    logger.error(
                        "email_attachments_too_large", draft_id=draft_id,
                        total=total_size, limit=max_total,
                    )
                    data["status"] = "error"
                    data["error"] = (
                        f"Вложения весят {total_size // (1024 * 1024)} МБ — "
                        f"больше лимита {max_total // (1024 * 1024)} МБ"
                    )
                    draft.draft_data = data
                    from sqlalchemy.orm.attributes import flag_modified as _fm1
                    _fm1(draft, "draft_data")
                    await db.commit()
                    return {"status": "error", "reason": "attachments_too_large"}

                if foreign:
                    logger.error(
                        "email_attachment_not_owned", draft_id=draft_id,
                        mailbox=mailbox_name, filenames=foreign,
                    )
                    data["status"] = "error"
                    data["error"] = f"Вложение недоступно: {', '.join(foreign)}"
                    draft.draft_data = data
                    from sqlalchemy.orm.attributes import flag_modified as _fm0
                    _fm0(draft, "draft_data")
                    await db.commit()
                    return {"status": "error", "reason": "attachment_not_owned"}

            try:
                from app.storage import download_file

                # Ф5.2 — картинка, вставленная в тело письма, должна прийти
                # получателю картинкой, а не ссылкой на наш сервер: ссылка
                # снаружи не открывается, а у получателя в письме дыра.
                # Разделяем по признаку: на что ссылается тело — то inline
                # (multipart/related, cid:), остальное — обычные вложения.
                from app.domain.email_inline import (
                    cid_for, rewrite_to_cid, split_inline,
                )

                inline_rows, file_rows = split_inline(body_html, list(att_rows))
                cid_by_id = {str(a.id): cid_for(a.id) for a in inline_rows}
                for a in inline_rows:
                    body_html = rewrite_to_cid(body_html, a.id)

                alt = MIMEMultipart("alternative")
                alt.attach(MIMEText(body_text, "plain", "utf-8"))
                if body_html:
                    alt.attach(MIMEText(body_html, "html", "utf-8"))

                body_part = alt
                if inline_rows:
                    related = MIMEMultipart("related")
                    related.attach(alt)
                    for a in inline_rows:
                        try:
                            payload_bytes = download_file(a.storage_path)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "email_inline_download_failed",
                                name=a.filename, error=str(exc),
                            )
                            continue
                        subtype = (a.content_type or "image/png").split("/")[-1]
                        part = MIMEImage(payload_bytes, _subtype=subtype)
                        part.add_header("Content-ID", f"<{cid_by_id[str(a.id)]}>")
                        part.add_header(
                            "Content-Disposition", "inline", filename=a.filename
                        )
                        related.attach(part)
                    body_part = related

                if file_rows:
                    msg = MIMEMultipart("mixed")
                    msg.attach(body_part)

                    for a in file_rows:
                        try:
                            payload_bytes = download_file(a.storage_path)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("email_attachment_download_failed", name=a.filename, error=str(exc))
                            continue
                        part = MIMEApplication(payload_bytes, _subtype="octet-stream")
                        part.add_header("Content-Disposition", "attachment", filename=a.filename)
                        msg.attach(part)
                else:
                    msg = body_part

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
                # Without a timeout a silent relay pins this worker forever.
                timeout = settings.smtp_timeout_seconds
                if not smtp_use_tls:
                    with smtplib.SMTP_SSL(
                        smtp_host, smtp_port, context=context, timeout=timeout
                    ) as server:
                        _authenticate(server)
                        server.sendmail(from_address, recipients, msg.as_string())
                else:
                    with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as server:
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

                # Commit "this went out" BEFORE any side effect. Everything
                # below (Sent copy, thread mirror, bus event, audit) is
                # bookkeeping; if one of them raises and rolls the transaction
                # back, `executed` reverts to False and the retry sends the
                # very same letter a SECOND time. Found exactly that way — an
                # un-awaited coroutine aborted the flush after a successful
                # delivery, leaving a delivered message marked unsent.
                await db.commit()

                # Ф2.4 — put a copy in the server's own Sent folder, so the
                # person's real mail client shows what this system sent. Not
                # fatal: the message HAS gone out, and failing the task here
                # would retry the SMTP send and deliver it twice.
                appended_uid = None
                try:
                    appended_uid = await _append_to_sent(
                        db, mailbox_name, msg.as_bytes(),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "email_append_to_sent_failed",
                        draft_id=draft_id, mailbox=mailbox_name, error=str(exc),
                    )
                    await db.rollback()
                    data["sent_folder_error"] = str(exc)[:300]
                    draft = await db.get(DraftAction, uuid.UUID(draft_id))
                    if draft is not None:
                        draft.draft_data = data
                        _fm(draft, "draft_data")
                        await db.commit()

                # Reflect the sent message into our own thread view + search.
                try:
                    await record_outbound_message(
                        db,
                        mailbox=mailbox_name or "",
                        draft_data=data,
                        smtp_message_id=smtp_message_id,
                        from_address=from_address,
                        imap_uid=appended_uid,
                    )
                except Exception as exc:  # noqa: BLE001
                    # The letter HAS gone out; we simply failed to mirror it
                    # into our own Sent view. Previously a warning and nothing
                    # else, so the message was invisible to its author forever.
                    # Flag it on the draft and tell them, instead of pretending.
                    logger.error(
                        "email_outbound_record_failed", draft_id=draft_id, error=str(exc)
                    )
                    await db.rollback()
                    data["outbound_record_error"] = str(exc)[:300]
                    draft = await db.get(DraftAction, uuid.UUID(draft_id))
                    if draft is not None:
                        draft.draft_data = data
                        _fm(draft, "draft_data")
                    await _notify_send_failure(
                        db, draft, to_addresses, subject,
                        "письмо отправлено, но не попало в «Отправленные» — "
                        f"сообщите администратору: {exc}",
                    )

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

                from app.core.metrics import email_sent_total

                email_sent_total.labels(outcome="sent").inc()
                logger.info("email_sent_smtp", draft_id=draft_id, to=to_addresses)
                return {"status": "sent"}

            except smtplib.SMTPException as exc:
                logger.error("smtp_error", draft_id=draft_id, error=str(exc))
                # Ф4 — a send that runs out of retries used to end in the log
                # and nowhere else: the composer said "queued" forever and the
                # author never learned the letter had not gone out.
                if self.request.retries >= self.max_retries:
                    data["status"] = "error"
                    data["error"] = str(exc)[:500]
                    draft.draft_data = data
                    from sqlalchemy.orm.attributes import flag_modified as _fm2

                    _fm2(draft, "draft_data")
                    await db.commit()
                    await _notify_send_failure(db, draft, to_addresses, subject, str(exc))
                    from app.core.metrics import email_sent_total

                    email_sent_total.labels(outcome="error").inc()
                    return {"status": "error", "reason": "smtp_failed",
                            "detail": str(exc)[:200]}
                raise self.retry(exc=exc)

    return run_async(_run())


async def _notify_send_failure(db, draft, to_addresses, subject, error: str) -> None:
    """Tell the author their message did not go out, with a way back to it."""
    from app.db.models import NotificationType
    from app.services.notifications import create_notification

    owner = (draft.draft_data or {}).get("created_by_sub")
    if not owner:
        return
    try:
        await create_notification(
            db,
            user_sub=owner,
            type=NotificationType.system,
            title="Письмо не отправлено",
            body=f"«{subject}» для {', '.join(to_addresses)[:120]}: {error[:200]}",
            entity_type="email_draft",
            entity_id=draft.id,
            action_url="/email?folder=drafts",
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("email_send_failure_notify_failed", error=str(exc))


async def _append_to_sent(db, mailbox_name: str | None, raw: bytes) -> int | None:
    """APPEND a just-sent message to the mailbox's Sent folder on the server.

    Returns the new UID when the server reports APPENDUID, so our own copy of
    the message becomes addressable for later flag sync — otherwise the letter
    would be the one thing in the mailbox we could never tell the server
    anything about.
    """
    if not mailbox_name:
        return None

    from sqlalchemy import select as _select

    from app.db.models import MailboxConfig, MailboxFolder
    from app.domain.imap_sync import append_to_folder

    folder = (
        await db.execute(
            _select(MailboxFolder.remote_name).where(
                MailboxFolder.mailbox == mailbox_name,
                MailboxFolder.local_folder == "sent",
            )
        )
    ).scalar_one_or_none()
    if not folder:
        # No Sent folder discovered yet — silently skipping is right here: the
        # message is delivered, and the folder map fills in on the next
        # email.discover_folders run.
        logger.info("email_sent_folder_unknown", mailbox=mailbox_name)
        return None

    config = (
        await db.execute(
            _select(MailboxConfig).where(MailboxConfig.name == mailbox_name)
        )
    ).scalar_one_or_none()
    if config is None or not config.imap_host:
        return None

    import asyncio as _asyncio

    from app.tasks.email_sync import _connect

    def _do() -> int | None:
        conn = _connect(config)
        try:
            return append_to_folder(conn, folder, raw)
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    return await _asyncio.to_thread(_do)
