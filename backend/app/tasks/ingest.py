"""Ingest tasks — IMAP polling, file storage, dedup, auto-linking."""

import base64
import dataclasses
import hashlib
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    Document,
    DocumentLink,
    DocumentStatus,
    EmailAttachment,
    EmailMessage,
    EmailThread,
    FileExtensionAllowlist,
    QuarantineEntry,
)
from app.domain.email_counts import invalidate_mailbox_counts_sync
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


def _mk_snippet(text: str | None) -> str:
    return " ".join((text or "").split())[:300]


def _sanitize_html(html: str | None) -> str | None:
    try:
        from app.domain.email_html import sanitize_email_html

        return sanitize_email_html(html)
    except Exception:  # noqa: BLE001
        return None


def _get_sync_session() -> Session:
    """Get a synchronous DB session for Celery tasks."""
    engine = create_engine(settings.database_url_sync, pool_pre_ping=True)
    return Session(engine)


def _record_sync_result(mailbox_name: str, error: str | None) -> None:
    """Persist the outcome of a poll onto MailboxConfig so the UI can show it.

    Success clears the error and stamps last_sync_at; failure records the error
    text and leaves last_sync_at untouched (last *successful* sync). Never let a
    bookkeeping failure mask the real poll result.
    """
    from app.db.models import MailboxConfig as MailboxConfigDB

    try:
        with _get_sync_session() as db:
            row = db.execute(
                select(MailboxConfigDB).where(MailboxConfigDB.name == mailbox_name)
            ).scalar_one_or_none()
            if row is None:
                return
            if error is None:
                row.last_sync_at = datetime.now(UTC)
                row.sync_error = None
            else:
                row.sync_error = error[:2000]
            db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("imap_sync_result_save_failed", mailbox=mailbox_name, error=str(e))


def _ingress_sender_authenticated(headers_meta: dict | None) -> bool:
    """Подтвердил ли принимающий сервер, что письмо действительно от этого
    отправителя.

    Заголовок ``From`` подделывается тривиально, а здесь он решал, исполнит ли
    агент поручение своими правами. Вердикты SPF/DKIM/DMARC мы уже разбираем
    при приёме (app.tasks.imap_client.parse_auth_results) — и до сих пор
    использовали их только для витрины провенанса счёта.

    Fail-closed: отсутствие заголовков — это «неизвестно», а не «прошло».
    Достаточно пройденного DMARC либо DKIM: DMARC уже включает выравнивание с
    доменом From, а один SPF проходит и на пересланном письме с чужим From.
    """
    auth = ((headers_meta or {}).get("auth") or {}) if isinstance(headers_meta, dict) else {}
    if not auth:
        return False
    if str(auth.get("dmarc") or "").lower() == "pass":
        return True
    return str(auth.get("dkim") or "").lower() == "pass"


def _ingress_sender_allowed(db: Session, mailbox: str, from_address: str) -> bool:
    """May this sender give the agent instructions by e-mail?

    Empty allowlist = the addresses of active users of this system. Anything
    else must be listed explicitly (full address or bare domain). Without this
    check, knowing the ingress address was enough to make the agent work for
    you — the messages arrive from outside and are executed with the agent's
    own permissions.

    Проверка подлинности отправителя — у вызывающего
    (:func:`_ingress_sender_authenticated`): список разрешённых адресов имеет
    смысл, только если адресу в ``From`` вообще можно верить.
    """
    from app.db.models import MailboxConfig, User

    addr = _bare_address(from_address).lower()
    if not addr:
        return False
    allowed = db.execute(
        select(MailboxConfig.ingress_allowed_senders).where(MailboxConfig.name == mailbox)
    ).scalar_one_or_none()
    entries = [str(e).strip().lower() for e in (allowed or []) if str(e).strip()]
    if entries:
        domain = addr.split("@")[-1]
        return addr in entries or domain in entries
    known = {
        (e or "").lower()
        for e in db.execute(
            select(User.email).where(User.is_active == True)  # noqa: E712
        )
        .scalars()
        .all()
    }
    return addr in known


def _bare_address(value: str) -> str:
    from email.utils import parseaddr

    return (parseaddr(value or "")[1] or "").strip()


def _poll_agent_instructions(mailbox: str, candidates: list[tuple]) -> int:
    """Turn qualifying messages into WorkOrders (app.domain.work_email_ingress).

    ``candidates`` is [(email_message_id, parsed)] — messages that have already
    been ingested normally (so they are visible in the mail client like any
    other) and passed both the subject-marker and the sender checks.
    """
    from app.tasks.async_runner import run_async

    async def _go() -> int:
        from app.db.session import _get_session_factory
        from app.domain.work_email_ingress import create_work_order_from_email

        created = 0
        async with _get_session_factory()() as db:
            for message_id, parsed in candidates:
                try:
                    await create_work_order_from_email(
                        db, parsed, mailbox=mailbox, email_message_pk=message_id
                    )
                    await db.commit()
                    created += 1
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    logger.error("agent_ingress_work_order_failed", error=str(exc))
        return created

    return run_async(_go())


def _inbound_folders(mailbox: str, primary: str) -> list[str]:
    """Remote folders whose new mail belongs in our inbox.

    The configured ``imap_folder`` always comes first; the rest are the
    discovered folders mapped to "inbox" and enabled. Folders the server uses
    for its own filing (INBOX/ToMyself, INBOX/Newsletters) are exactly the ones
    a plain INBOX poll never sees.
    """
    from app.db.models import MailboxFolder
    from app.db.sync_session import sync_session

    out = [primary]
    try:
        with sync_session() as db:
            rows = (
                db.execute(
                    select(MailboxFolder.remote_name).where(
                        MailboxFolder.mailbox == mailbox,
                        MailboxFolder.local_folder == "inbox",
                        MailboxFolder.sync_enabled == True,  # noqa: E712
                        MailboxFolder.is_selectable == True,  # noqa: E712
                    )
                )
                .scalars()
                .all()
            )
    except Exception as exc:  # noqa: BLE001
        # Never let folder discovery break the poll that already worked.
        logger.warning("imap_inbound_folders_failed", mailbox=mailbox, error=str(exc))
        return out
    for name in rows:
        if name not in out:
            out.append(name)
    return out


@celery_app.task(name="app.tasks.ingest.poll_imap_mailbox", bind=True, max_retries=3)
def poll_imap_mailbox(self, mailbox: str) -> dict:
    """Poll a single IMAP mailbox for new messages.

    The mailbox name is a MailboxConfig.name row; credentials (and OAuth tokens)
    come from that row. Records last_sync_at / sync_error on the row so the
    client can surface connection problems instead of showing an empty inbox.
    """
    import time as _time

    from app.core.metrics import (
        email_imap_errors_total,
        email_messages_ingested_total,
        email_poll_duration_seconds,
    )
    from app.tasks.imap_client import fetch_unseen_from_mailbox, get_mailbox_configs

    _poll_started = _time.monotonic()
    errors_early: list[str] = []
    logger.info("imap_poll_start", mailbox=mailbox)

    configs = get_mailbox_configs()
    config = next((c for c in configs if c.name == mailbox), None)
    if not config:
        logger.warning("imap_mailbox_not_configured", mailbox=mailbox)
        _record_sync_result(mailbox, "Ящик не найден среди активных конфигураций")
        return {
            "mailbox": mailbox,
            "fetched": 0,
            "documents": [],
            "errors": ["Mailbox not configured"],
        }

    # Ф2 — забирать письма из ВСЕХ папок, которые синкаются во «Входящие», а
    # не только из настроенной imap_folder. Сервер сам раскладывает входящие
    # по подпапкам (mail.ru кладёт письмо самому себе в INBOX/ToMyself,
    # провайдеры автосортируют в INBOX/Newsletters), и такое письмо не
    # появлялось здесь никогда — при этом нигде не возникало ошибки.
    folders = _inbound_folders(mailbox, config.folder)
    emails = []
    for remote in folders:
        try:
            if remote == config.folder:
                per_folder = config
            else:
                from app.tasks.imap_client import folder_last_seen_uid

                per_folder = dataclasses.replace(
                    config,
                    folder=remote,
                    watermark_folder=remote,
                    last_seen_uid=folder_last_seen_uid(mailbox, remote),
                )
            emails.extend(fetch_unseen_from_mailbox(per_folder))
        except Exception as e:
            if remote == config.folder:
                # The primary folder failing is a connection problem, not a
                # quirk of one auto-sorted subfolder.
                logger.error("imap_fetch_failed", mailbox=mailbox, error=str(e))
                email_imap_errors_total.labels(mailbox=mailbox, stage="poll").inc()
                _record_sync_result(mailbox, f"IMAP: {e}")
                raise self.retry(countdown=60, exc=e)
            logger.warning(
                "imap_subfolder_fetch_failed",
                mailbox=mailbox,
                folder=remote,
                error=str(e),
            )
            errors_early.append(f"{remote}: {e}")

    errors: list[str] = list(errors_early)
    created_doc_ids: list[str] = []
    mailbox_owner: str | None = None

    # A mailbox (or IMAP subfolder) dedicated to agent instructions. Set up by
    # an admin as a separate MailboxConfig row with a dedicated folder so it
    # never races the invoice poller for the same unseen messages.
    #
    # Ф0.6: this used to short-circuit the whole pipeline — messages became
    # WorkOrders and were never stored as EmailMessage, so nothing was visible
    # in the mail client and there was no record of what the agent was told to
    # do. And every message became an instruction: the subject marker check
    # (work_email_ingress.is_agent_instruction_email) that the function's own
    # docstring requires of its caller was never performed, so spam to that
    # address became agent tasks. Now such a mailbox is ingested like any
    # other, and qualifying messages additionally spawn a WorkOrder.
    is_agent_ingress = (config.assigned_role or "") == "agent_ingress"
    ingress_candidates: list[tuple] = []
    queued_for_extraction = 0
    triaged = 0
    # Messages whose notification is deferred to the triage result (Ф6.7).
    pending_notify: list = []

    with _get_sync_session() as db:
        # Resolved once per poll: attachments of a personal mailbox inherit its
        # owner (see _store_attachment).
        mailbox_owner = _mailbox_owner_sub(db, mailbox)
        process_attachments, auto_approve, triage_mode = _mailbox_automation(db, mailbox)
        for parsed in emails:
            try:
                # Check for duplicate by Message-ID
                if parsed.message_id:
                    existing = db.execute(
                        select(EmailMessage).where(
                            EmailMessage.message_id_header == parsed.message_id
                        )
                    ).scalar_one_or_none()
                    if existing:
                        logger.info("imap_duplicate_message", message_id=parsed.message_id)
                        continue

                # Thread detection
                thread_id = _find_or_create_thread(db, parsed.subject, parsed.in_reply_to, mailbox)

                # Create EmailMessage
                email_msg = EmailMessage(
                    thread_id=thread_id,
                    message_id_header=parsed.message_id,
                    in_reply_to=parsed.in_reply_to,
                    imap_uid=getattr(parsed, "imap_uid", None),
                    imap_folder=getattr(parsed, "imap_folder", None),
                    references=getattr(parsed, "references", None),
                    reply_to=getattr(parsed, "reply_to", None),
                    headers_meta=getattr(parsed, "headers_meta", None) or None,
                    mailbox=mailbox,
                    from_address=parsed.from_address,
                    to_addresses=parsed.to_addresses,
                    cc_addresses=parsed.cc_addresses,
                    subject=parsed.subject,
                    body_text=parsed.body_text,
                    body_html=parsed.body_html,
                    sent_at=parsed.sent_at,
                    received_at=datetime.now(UTC),
                    has_attachments=parsed.has_attachments,
                    attachment_count=sum(
                        1 for a in parsed.attachments if not getattr(a, "is_inline", False)
                    ),
                    attachments_meta=[
                        {"filename": a.filename, "size": a.size, "content_type": a.content_type}
                        for a in parsed.attachments
                        if not getattr(a, "is_inline", False)
                    ],
                    is_inbound=True,
                    is_read=False,
                    folder="inbox",
                    snippet=_mk_snippet(parsed.body_text),
                    body_text_derived=getattr(parsed, "body_text_derived", False),
                    # cid: rewriting happens after the row has an id (below).
                    body_html_sanitized=_sanitize_html(parsed.body_html),
                )
                db.add(email_msg)
                db.flush()
                email_messages_ingested_total.labels(mailbox=mailbox).inc()

                # Inline images can only be addressed once the message row has
                # an id, so the cid: rewrite happens here rather than above.
                if email_msg.body_html_sanitized:
                    from app.domain.email_html import block_remote_images, rewrite_cid_images

                    if "cid:" in email_msg.body_html_sanitized.lower():
                        email_msg.body_html_sanitized = rewrite_cid_images(
                            email_msg.body_html_sanitized, email_msg.id
                        )
                    # Remote images are read receipts; the reader decides
                    # whether to load them (Ф1.4). The URL survives in
                    # data-blocked-src so "показать" is a client-side swap.
                    email_msg.body_html_sanitized, _blocked = block_remote_images(
                        email_msg.body_html_sanitized
                    )

                # Process attachments: always a normalised EmailAttachment row
                # (raw-byte access for the agent, outbound reuse); Documents are
                # still created only for recognisable types inside _store_attachment.
                message_doc_ids: list[str] = []
                for att in parsed.attachments:
                    doc = _store_attachment(
                        db,
                        att,
                        email_msg.id,
                        mailbox,
                        owner_sub=mailbox_owner,
                        auto_approve=auto_approve,
                    )
                    if doc:
                        created_doc_ids.append(str(doc.id))
                        # Quarantined files are not recognised — they are held
                        # for a human on purpose.
                        if doc.status != DocumentStatus.suspicious:
                            message_doc_ids.append(str(doc.id))

                # Roll thread-level client state forward.
                thread = db.get(EmailThread, email_msg.thread_id) if email_msg.thread_id else None
                if thread is not None:
                    thread.is_read = False
                    thread.unread_count = (thread.unread_count or 0) + 1
                    thread.last_snippet = email_msg.snippet
                    thread.folder = "inbox"
                    if parsed.attachments:
                        thread.has_attachments = True
                db.flush()

                # Server-side filter rules (labels / move / run extraction /
                # forward to the agent). Runs after attachments exist so an
                # attachment-condition or run_extraction action can see them.
                try:
                    from app.domain.email_rules import apply_rules

                    apply_rules(db, email_msg, mailbox)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("email_rules_failed", mailbox=mailbox, error=str(exc))

                if is_agent_ingress:
                    from app.domain.work_email_ingress import is_agent_instruction_email

                    if not is_agent_instruction_email(parsed):
                        logger.info(
                            "agent_ingress_skipped_no_marker",
                            mailbox=mailbox,
                            subject=parsed.subject,
                        )
                    elif not _ingress_sender_authenticated(parsed.headers_meta):
                        logger.warning(
                            "agent_ingress_sender_unauthenticated",
                            mailbox=mailbox,
                            sender=parsed.from_address,
                            auth=(parsed.headers_meta or {}).get("auth"),
                        )
                    elif not _ingress_sender_allowed(db, mailbox, parsed.from_address):
                        logger.warning(
                            "agent_ingress_sender_not_allowed",
                            mailbox=mailbox,
                            sender=parsed.from_address,
                        )
                    else:
                        ingress_candidates.append((email_msg.id, parsed))

                # Ф6.7 — when the agent is going to look at this letter, the
                # useful notification is what it FOUND, not that mail arrived.
                # Two pings for one letter ("новое письмо" then "разобрала
                # счёт") is how people learn to ignore notifications.
                if triage_mode == "full":
                    pending_notify.append(email_msg.id)
                else:
                    _notify_new_email(db, mailbox, email_msg)

                db.commit()

                # Ф6.1 — the missing link. _store_attachment created a Document
                # and stopped there: process_document was only ever reached via
                # a run_extraction filter rule, a manual API call, or run_triage
                # (which is not in the beat schedule). So "счёт пришёл письмом →
                # он в системе" simply did not happen. Dispatch AFTER commit, or
                # the worker can pick the id up before the row is visible.
                # Ф6.4 — understand the LETTER itself, above and beside the
                # attachment pipeline below.
                if triage_mode != "off":
                    try:
                        from app.tasks.email_triage import triage_message

                        triage_message.apply_async(args=[str(email_msg.id)], queue="mail")
                        triaged += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "email_triage_dispatch_failed",
                            message_id=str(email_msg.id),
                            error=str(exc),
                        )

                if process_attachments:
                    from app.tasks.extraction import process_document

                    for doc_id in message_doc_ids:
                        try:
                            process_document.apply_async(args=[doc_id], queue="extraction")
                            queued_for_extraction += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.error(
                                "attachment_extraction_dispatch_failed",
                                document_id=doc_id,
                                error=str(exc),
                            )

            except IntegrityError:
                # The partial unique index on message_id_header (Ф1.5) turns a
                # lost dedup race into this, not into a duplicated letter.
                db.rollback()
                logger.info("imap_duplicate_message_race", message_id=parsed.message_id)
            except Exception as e:
                db.rollback()
                logger.error("imap_process_error", error=str(e), subject=parsed.subject)
                errors.append(f"Error processing '{parsed.subject}': {e}")

    work_orders = _poll_agent_instructions(mailbox, ingress_candidates) if ingress_candidates else 0

    # If triage never got to them (queue down, model unavailable), the letters
    # must still be announced — silence is the one outcome worse than a
    # duplicate ping.
    if pending_notify:
        _notify_untriaged_after_delay.apply_async(
            args=[mailbox, [str(m) for m in pending_notify]],
            countdown=180,
            queue="mail",
        )

    if emails:
        # New mail changes every sidebar counter; the cache must not outlive it.
        invalidate_mailbox_counts_sync()

    logger.info(
        "imap_poll_complete",
        mailbox=mailbox,
        emails=len(emails),
        documents=len(created_doc_ids),
        queued_for_extraction=queued_for_extraction,
        triaged=triaged,
        work_orders=work_orders,
        errors=len(errors),
    )
    # A per-message processing error is not a connection failure — the mailbox
    # is reachable, so record a successful sync but keep the errors in the
    # result for the triage aggregator / caller.
    _record_sync_result(mailbox, None)
    email_poll_duration_seconds.observe(_time.monotonic() - _poll_started)
    return {
        "mailbox": mailbox,
        "fetched": len(emails),
        "documents": created_doc_ids,
        "queued_for_extraction": queued_for_extraction,
        "triaged": triaged,
        "work_orders": work_orders,
        "errors": errors,
    }


def _mailbox_automation(db: Session, mailbox: str) -> tuple[bool, bool, str]:
    """(process_attachments, auto_approve_invoices, agent_triage_mode) — Ф6.1/6.4.

    A personal mailbox is additionally gated by its owner's ``sweep_enabled``
    consent: recognising the contents of someone's private mail is exactly the
    thing that consent covers.
    """
    from app.db.models import MailboxConfig

    row = db.execute(
        select(
            MailboxConfig.auto_process_attachments,
            MailboxConfig.auto_approve_invoices,
            MailboxConfig.mailbox_type,
            MailboxConfig.sweep_enabled,
            MailboxConfig.agent_triage_mode,
        ).where(MailboxConfig.name == mailbox)
    ).first()
    if row is None:
        return True, False, "classify"
    process, auto_approve, mailbox_type, sweep, triage_mode = row
    if mailbox_type == "personal" and not sweep:
        # No consent — the agent neither recognises nor reads this mailbox.
        return False, False, "off"
    return bool(process), bool(auto_approve), (triage_mode or "classify")


def _mailbox_recipients(db: Session, mailbox: str) -> list[str]:
    """Resolve which users to notify about a new email in `mailbox`.

    Personal mailbox → its owner, and nobody else. The admin fallback below is
    right for a company inbox nobody claimed, and completely wrong for private
    mail: it would push every employee's personal correspondence to every admin.

    Shared mailbox → users whose role matches `assigned_role`; if none is
    configured or matches, fall back to active admins.
    """
    from app.db.models import MailboxConfig, User

    cfg = db.execute(
        select(
            MailboxConfig.assigned_role, MailboxConfig.mailbox_type, MailboxConfig.owner_sub
        ).where(MailboxConfig.name == mailbox)
    ).first()
    role = cfg[0] if cfg else None
    if role == "agent_ingress":
        # Not a user role — it marks the agent's instruction channel. Matching
        # it against User.role finds nobody and silently fell through to
        # "notify every admin".
        role = None
    if cfg and cfg[1] == "personal":
        return [cfg[2]] if cfg[2] else []

    subs: list[str] = []
    if role:
        subs = list(
            db.execute(
                select(User.sub).where(User.role == role, User.is_active == True)  # noqa: E712
            )
            .scalars()
            .all()
        )
    if not subs:
        subs = list(
            db.execute(
                select(User.sub).where(User.role == "admin", User.is_active == True)  # noqa: E712
            )
            .scalars()
            .all()
        )
    return subs


def _notify_new_email(db: Session, mailbox: str, email_msg) -> None:
    """Create an in-app + push notification for a freshly ingested inbound email."""
    from app.db.models import MailboxConfig as _MBC
    from app.db.models import NotificationType
    from app.services.notifications import create_notification_sync

    sender = email_msg.from_address or "—"
    subject = (email_msg.subject or "(без темы)").strip()
    title = f"Новое письмо · {mailbox}"
    body = f"{sender}: {subject}"[:480]
    # Private mail must not put sender+subject on a phone's lock screen by
    # default (Ф0.8); the in-app notification, which is behind the session,
    # keeps the preview either way.
    is_personal = (
        db.execute(select(_MBC.mailbox_type).where(_MBC.name == mailbox)).scalar_one_or_none()
        == "personal"
    )
    recipients: list[str] = []
    try:
        recipients = _mailbox_recipients(db, mailbox)
        for sub in recipients:
            create_notification_sync(
                db,
                user_sub=sub,
                type=NotificationType.email_received,
                title=title,
                body=body,
                entity_type="email",
                entity_id=email_msg.id,
                # Deep-link straight to the conversation (Ф7.3 needs this too);
                # "/email" made the user hunt for the letter they were pinged about.
                action_url=(f"/email/{email_msg.thread_id}" if email_msg.thread_id else "/email"),
                private_preview=is_personal,
            )
    except Exception as e:  # never block ingestion on notification errors
        logger.warning("email_notify_failed", mailbox=mailbox, error=str(e))

    # Real-time nudge for the mail client (best-effort, never blocks ingest).
    try:
        from app.core.chat_bus import publish_sync

        event = {
            "type": "email.new",
            "mailbox": mailbox,
            "thread_id": str(email_msg.thread_id) if email_msg.thread_id else None,
            "subject": subject,
            "from": sender,
            "snippet": (email_msg.body_text or "")[:200],
        }
        for sub in recipients:
            publish_sync(event, user_sub=sub)
    except Exception as e:  # noqa: BLE001
        logger.warning("email_realtime_publish_failed", mailbox=mailbox, error=str(e))


def _find_or_create_thread(
    db: Session, subject: str, in_reply_to: str | None, mailbox: str
) -> uuid.UUID | None:
    """Find existing thread or create new one.

    Threading logic:
    1. If In-Reply-To header → find message with that Message-ID → use its thread
    2. If subject starts with Re:/Fwd: → strip and find thread by subject
    3. Otherwise → create new thread
    """
    # Try In-Reply-To
    if in_reply_to:
        parent = db.execute(
            select(EmailMessage).where(EmailMessage.message_id_header == in_reply_to)
        ).scalar_one_or_none()
        if parent and parent.thread_id:
            # Update thread stats
            thread = db.get(EmailThread, parent.thread_id)
            if thread:
                thread.message_count += 1
                thread.last_message_at = datetime.now(UTC)
            return parent.thread_id

    # Try subject matching (strip Re:/Fwd:)
    clean_subject = subject
    for prefix in ("Re:", "RE:", "Fwd:", "FWD:", "Fw:", "FW:"):
        clean_subject = clean_subject.removeprefix(prefix).strip()

    if clean_subject:
        existing_thread = db.execute(
            select(EmailThread).where(
                EmailThread.subject == clean_subject,
                EmailThread.mailbox == mailbox,
            )
        ).scalar_one_or_none()
        if existing_thread:
            existing_thread.message_count += 1
            existing_thread.last_message_at = datetime.now(UTC)
            return existing_thread.id

    # Create new thread
    thread = EmailThread(
        subject=clean_subject or subject,
        mailbox=mailbox,
        message_count=1,
        last_message_at=datetime.now(UTC),
    )
    db.add(thread)
    db.flush()
    return thread.id


def _mailbox_owner_sub(db: Session, mailbox: str) -> str | None:
    """Owner of a personal mailbox (None for shared/company inboxes)."""
    from app.db.models import MailboxConfig

    row = db.execute(
        select(MailboxConfig.mailbox_type, MailboxConfig.owner_sub).where(
            MailboxConfig.name == mailbox
        )
    ).first()
    return row[1] if row and row[0] == "personal" else None


def _store_attachment(
    db: Session,
    att,
    email_message_id: uuid.UUID,
    mailbox: str,
    owner_sub: str | None = None,
    auto_approve: bool = False,
) -> Document | None:
    """Store email attachment as Document.

    1. Compute SHA-256
    2. Check dedup
    3. Upload to MinIO
    4. Create Document + link to EmailMessage
    """
    file_hash = att.sha256
    storage_path = f"documents/{file_hash[:2]}/{file_hash[2:4]}/{file_hash}"
    is_allowed = _is_extension_allowed(db, att.filename)

    # Always keep a normalised attachment row + raw bytes: the agent needs
    # on-demand access (email.get_attachment / recognize_attachment) and
    # outbound mail reuses stored files. Bytes go to the shared documents/
    # object-store path (same key a Document would use) so no duplication.
    email_att = EmailAttachment(
        message_id=email_message_id,
        filename=att.filename,
        content_type=att.content_type,
        size=att.size,
        storage_path=storage_path,
        sha256=file_hash,
        content_id=getattr(att, "content_id", None),
        is_inline=getattr(att, "is_inline", False),
    )
    db.add(email_att)
    try:
        from app.storage import upload_file as _upload_att

        _upload_att(att.content, storage_path, att.content_type)
    except Exception as e:  # noqa: BLE001
        # Ф1.5: do NOT leave storage_path pointing at bytes that were never
        # written — the row then looks fine and every later download 502s.
        email_att.storage_path = None
        logger.warning("minio_attachment_upload_failed", filename=att.filename, error=str(e))
        return None

    # A signature logo referenced by cid: is not a document to recognise.
    if getattr(att, "is_inline", False):
        return None

    # Dedup check. Deliberately scoped: a private attachment must never be
    # de-duplicated into a shared document (that would hand a colleague's file to
    # everyone), and a shared document must not be narrowed to one owner. Only
    # documents with the same ownership are reused.
    existing = (
        db.execute(
            select(Document).where(
                Document.file_hash == file_hash,
                Document.owner_sub.is_(None)
                if owner_sub is None
                else Document.owner_sub == owner_sub,
            )
        )
        .scalars()
        .first()
    )
    if existing:
        logger.info("attachment_duplicate", filename=att.filename, hash=file_hash)
        email_att.document_id = existing.id
        link = DocumentLink(
            document_id=existing.id,
            linked_entity_type="email_message",
            linked_entity_id=email_message_id,
            link_type="attachment",
        )
        db.add(link)
        return None

    # Create Document (bytes already uploaded above)
    doc = Document(
        file_name=att.filename,
        file_hash=file_hash,
        file_size=att.size,
        mime_type=att.content_type,
        storage_path=storage_path,
        source_channel="email",
        source_email_id=email_message_id,
        # Ф6.1: mail defaults to "auto up to Needs Review" — a human approves.
        # The per-document flag is what tasks/extraction.py::extract_invoice
        # reads, so this overrides the global auto_verify only for e-mail, and
        # only when the mailbox has not opted into auto-approval.
        metadata_={"auto_verify": bool(auto_approve)},
        # Attachments from a personal mailbox stay owned by that employee, so the
        # existing row-level visibility (app/domain/access.py) keeps them out of
        # the company-wide document flow, search and RAG.
        owner_sub=owner_sub,
        status=DocumentStatus.ingested if is_allowed else DocumentStatus.suspicious,
    )
    db.add(doc)
    db.flush()
    email_att.document_id = doc.id

    if not is_allowed:
        db.add(
            QuarantineEntry(
                document_id=doc.id,
                reason="extension_not_allowed",
                original_filename=att.filename,
                detected_mime=att.content_type,
            )
        )

    # Link to email
    link = DocumentLink(
        document_id=doc.id,
        linked_entity_type="email_message",
        linked_entity_id=email_message_id,
        link_type="attachment",
    )
    db.add(link)

    logger.info(
        "attachment_stored",
        doc_id=str(doc.id),
        filename=att.filename,
        mailbox=mailbox,
        quarantined=not is_allowed,
    )
    return doc


def _is_extension_allowed(db: Session, filename: str) -> bool:
    """Same rule as the upload endpoint, including its fallback.

    The DB allowlist is an override; when it says nothing about this extension
    the shared default set decides. Without the fallback an empty table meant
    "quarantine everything", and an emailed invoice never reached recognition
    while the identical file uploaded by hand did.
    """
    from app.domain.file_types import DEFAULT_ALLOWED_EXTENSIONS, extension_of

    extension = extension_of(filename)
    if not extension:
        return False
    row = db.execute(
        select(FileExtensionAllowlist).where(FileExtensionAllowlist.extension == extension)
    ).scalar_one_or_none()
    if row is not None:
        return bool(row.is_allowed)
    return extension in DEFAULT_ALLOWED_EXTENSIONS


@celery_app.task(name="app.tasks.ingest.store_document", bind=True)
def store_document(
    self,
    file_content_b64: str,
    file_name: str,
    mime_type: str,
    source_channel: str = "upload",
    source_email_id: str | None = None,
) -> dict:
    """Store a document in MinIO and create DB record."""
    content = base64.b64decode(file_content_b64)
    file_hash = hashlib.sha256(content).hexdigest()
    storage_path = f"documents/{file_hash[:2]}/{file_hash[2:4]}/{file_hash}"

    logger.info("store_document", file_name=file_name, file_hash=file_hash, size=len(content))

    # Upload to MinIO
    try:
        from app.storage import upload_file

        upload_file(content, storage_path, mime_type)
    except Exception as e:
        logger.warning("minio_upload_failed", error=str(e))

    # Create DB record
    with _get_sync_session() as db:
        # Dedup
        existing = db.execute(
            select(Document).where(Document.file_hash == file_hash)
        ).scalar_one_or_none()
        if existing:
            return {
                "document_id": str(existing.id),
                "file_hash": file_hash,
                "storage_path": storage_path,
                "is_duplicate": True,
            }

        doc = Document(
            file_name=file_name,
            file_hash=file_hash,
            file_size=len(content),
            mime_type=mime_type,
            storage_path=storage_path,
            source_channel=source_channel,
            source_email_id=uuid.UUID(source_email_id) if source_email_id else None,
            status=DocumentStatus.ingested,
        )
        db.add(doc)
        db.commit()

        doc_id = str(doc.id)

    # Trigger auto-linking
    auto_link_document.delay(doc_id)

    return {
        "document_id": doc_id,
        "file_hash": file_hash,
        "storage_path": storage_path,
        "is_duplicate": False,
    }


@celery_app.task(name="app.tasks.ingest.auto_link_document")
def auto_link_document(document_id: str) -> dict:
    """Auto-link document to related entities.

    Heuristics:
    1. Same email thread → link to thread
    2. Similar subject → link to related documents
    3. Same file hash → mark as duplicate version
    4. Supplier mention in filename → link to party
    """
    logger.info("auto_link", document_id=document_id)

    doc_uuid = uuid.UUID(document_id)
    links_created = 0

    with _get_sync_session() as db:
        doc = db.get(Document, doc_uuid)
        if not doc:
            return {"document_id": document_id, "links_created": 0}

        # If from email — link to email thread
        if doc.source_email_id:
            email_msg = db.get(EmailMessage, doc.source_email_id)
            if email_msg and email_msg.thread_id:
                existing_link = db.execute(
                    select(DocumentLink).where(
                        DocumentLink.document_id == doc.id,
                        DocumentLink.linked_entity_type == "email_thread",
                        DocumentLink.linked_entity_id == email_msg.thread_id,
                    )
                ).scalar_one_or_none()
                if not existing_link:
                    link = DocumentLink(
                        document_id=doc.id,
                        linked_entity_type="email_thread",
                        linked_entity_id=email_msg.thread_id,
                        link_type="from_thread",
                    )
                    db.add(link)
                    links_created += 1

        db.commit()

    return {"document_id": document_id, "links_created": links_created}


@celery_app.task(name="email.notify_untriaged", bind=True, queue="mail")
def _notify_untriaged_after_delay(self, mailbox: str, message_ids: list[str]) -> dict:
    """Fallback for Ф6.7: announce letters the triage never reported on.

    The plain "новое письмо" ping is suppressed when the agent is going to
    summarise the letter itself. If that never happens — queue backed up, model
    down, task lost — the letter would otherwise arrive in total silence, which
    is a worse failure than a duplicate notification.
    """
    from sqlalchemy import select as _select

    from app.db.models import EmailMessage, EmailTriageResult
    from app.db.sync_session import sync_session

    notified = 0
    with sync_session() as db:
        for raw_id in message_ids:
            try:
                msg_id = uuid.UUID(raw_id)
            except (TypeError, ValueError):
                continue
            triaged = db.execute(
                _select(EmailTriageResult.id).where(
                    EmailTriageResult.message_id == msg_id,
                    EmailTriageResult.status == "done",
                )
            ).first()
            if triaged:
                continue  # the agent already told them what it found
            msg = db.get(EmailMessage, msg_id)
            if msg is None:
                continue
            _notify_new_email(db, mailbox, msg)
            notified += 1
        db.commit()

    if notified:
        logger.info("email_untriaged_notified", mailbox=mailbox, count=notified)
    return {"status": "ok", "notified": notified}


async def _notify_new_email_async(db, mailbox: str, email_msg) -> None:
    """Async twin of _notify_new_email — the triage path runs on AsyncSession.

    Kept next to its sync sibling on purpose: two notification texts drifting
    apart is how "новое письмо" and "Света разобрала" end up describing the
    same letter differently.
    """
    from sqlalchemy import select as _select

    from app.db.models import MailboxConfig, NotificationType, User
    from app.services.notifications import create_notification

    sender = email_msg.from_address or "—"
    subject = (email_msg.subject or "(без темы)").strip()
    row = (
        await db.execute(
            _select(
                MailboxConfig.assigned_role, MailboxConfig.mailbox_type, MailboxConfig.owner_sub
            ).where(MailboxConfig.name == mailbox)
        )
    ).first()
    if row is None:
        return
    role, mailbox_type, owner_sub = row
    if mailbox_type == "personal":
        subs = [owner_sub] if owner_sub else []
    elif role and role != "agent_ingress":
        subs = list(
            (
                await db.execute(
                    _select(User.sub).where(User.role == role, User.is_active == True)  # noqa: E712
                )
            )
            .scalars()
            .all()
        )
    else:
        subs = list(
            (
                await db.execute(
                    _select(User.sub).where(User.role == "admin", User.is_active == True)  # noqa: E712
                )
            )
            .scalars()
            .all()
        )

    for sub in subs:
        await create_notification(
            db,
            user_sub=sub,
            type=NotificationType.email_received,
            title=f"Новое письмо · {mailbox}",
            body=f"{sender}: {subject}"[:480],
            entity_type="email",
            entity_id=email_msg.id,
            action_url=(f"/email/{email_msg.thread_id}" if email_msg.thread_id else "/email"),
            private_preview=(mailbox_type == "personal"),
        )
