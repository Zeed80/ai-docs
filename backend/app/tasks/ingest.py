"""Ingest tasks — IMAP polling, file storage, dedup, auto-linking."""

import base64
import hashlib
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.base import Base
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
                row.last_sync_at = datetime.now(timezone.utc)
                row.sync_error = None
            else:
                row.sync_error = error[:2000]
            db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("imap_sync_result_save_failed", mailbox=mailbox_name, error=str(e))


def _poll_agent_instructions(mailbox: str, emails) -> dict:
    """Turn each fetched message into a WorkOrder (app.domain.work_email_ingress)."""
    from app.tasks.async_runner import run_async

    async def _go() -> dict:
        from app.db.session import _get_session_factory
        from app.domain.work_email_ingress import create_work_order_from_email

        created = 0
        async with _get_session_factory()() as db:
            for parsed in emails:
                try:
                    await create_work_order_from_email(db, parsed)
                    await db.commit()
                    created += 1
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    logger.error("agent_ingress_work_order_failed", error=str(exc))
        return {"mailbox": mailbox, "fetched": len(emails), "work_orders": created, "documents": []}

    return run_async(_go())


@celery_app.task(name="app.tasks.ingest.poll_imap_mailbox", bind=True, max_retries=3)
def poll_imap_mailbox(self, mailbox: str) -> dict:
    """Poll a single IMAP mailbox for new messages.

    The mailbox name is a MailboxConfig.name row; credentials (and OAuth tokens)
    come from that row. Records last_sync_at / sync_error on the row so the
    client can surface connection problems instead of showing an empty inbox.
    """
    from app.tasks.imap_client import get_mailbox_configs, fetch_unseen_from_mailbox

    logger.info("imap_poll_start", mailbox=mailbox)

    configs = get_mailbox_configs()
    config = next((c for c in configs if c.name == mailbox), None)
    if not config:
        logger.warning("imap_mailbox_not_configured", mailbox=mailbox)
        _record_sync_result(mailbox, "Ящик не найден среди активных конфигураций")
        return {"mailbox": mailbox, "fetched": 0, "documents": [], "errors": ["Mailbox not configured"]}

    try:
        emails = fetch_unseen_from_mailbox(config)
    except Exception as e:
        logger.error("imap_fetch_failed", mailbox=mailbox, error=str(e))
        _record_sync_result(mailbox, f"IMAP: {e}")
        raise self.retry(countdown=60, exc=e)

    errors: list[str] = []
    created_doc_ids: list[str] = []
    mailbox_owner: str | None = None

    # A mailbox (or IMAP subfolder) dedicated to agent instructions: every
    # message becomes a WorkOrder instead of going through invoice triage. Set
    # up by an admin as a separate MailboxConfig row with a dedicated folder so
    # it never races the invoice poller for the same unseen messages.
    if (config.assigned_role or "") == "agent_ingress":
        _record_sync_result(mailbox, None)
        return _poll_agent_instructions(mailbox, emails)

    with _get_sync_session() as db:
        # Resolved once per poll: attachments of a personal mailbox inherit its
        # owner (see _store_attachment).
        mailbox_owner = _mailbox_owner_sub(db, mailbox)
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
                thread_id = _find_or_create_thread(
                    db, parsed.subject, parsed.in_reply_to, mailbox
                )

                # Create EmailMessage
                email_msg = EmailMessage(
                    thread_id=thread_id,
                    message_id_header=parsed.message_id,
                    in_reply_to=parsed.in_reply_to,
                    mailbox=mailbox,
                    from_address=parsed.from_address,
                    to_addresses=parsed.to_addresses,
                    cc_addresses=parsed.cc_addresses,
                    subject=parsed.subject,
                    body_text=parsed.body_text,
                    body_html=parsed.body_html,
                    sent_at=parsed.sent_at,
                    received_at=datetime.now(timezone.utc),
                    has_attachments=parsed.has_attachments,
                    attachment_count=len(parsed.attachments),
                    attachments_meta=[
                        {"filename": a.filename, "size": a.size, "content_type": a.content_type}
                        for a in parsed.attachments
                    ],
                    is_inbound=True,
                    is_read=False,
                    folder="inbox",
                    snippet=_mk_snippet(parsed.body_text),
                    body_html_sanitized=_sanitize_html(parsed.body_html),
                )
                db.add(email_msg)
                db.flush()

                # Process attachments: always a normalised EmailAttachment row
                # (raw-byte access for the agent, outbound reuse); Documents are
                # still created only for recognisable types inside _store_attachment.
                for att in parsed.attachments:
                    doc = _store_attachment(db, att, email_msg.id, mailbox, owner_sub=mailbox_owner)
                    if doc:
                        created_doc_ids.append(str(doc.id))

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

                _notify_new_email(db, mailbox, email_msg)

                db.commit()

            except Exception as e:
                db.rollback()
                logger.error("imap_process_error", error=str(e), subject=parsed.subject)
                errors.append(f"Error processing '{parsed.subject}': {e}")

    logger.info(
        "imap_poll_complete",
        mailbox=mailbox,
        emails=len(emails),
        documents=len(created_doc_ids),
        errors=len(errors),
    )
    # A per-message processing error is not a connection failure — the mailbox
    # is reachable, so record a successful sync but keep the errors in the
    # result for the triage aggregator / caller.
    _record_sync_result(mailbox, None)
    return {"mailbox": mailbox, "fetched": len(emails), "documents": created_doc_ids, "errors": errors}


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
        select(MailboxConfig.assigned_role, MailboxConfig.mailbox_type, MailboxConfig.owner_sub)
        .where(MailboxConfig.name == mailbox)
    ).first()
    role = cfg[0] if cfg else None
    if cfg and cfg[1] == "personal":
        return [cfg[2]] if cfg[2] else []

    subs: list[str] = []
    if role:
        subs = list(
            db.execute(
                select(User.sub).where(User.role == role, User.is_active == True)  # noqa: E712
            ).scalars().all()
        )
    if not subs:
        subs = list(
            db.execute(
                select(User.sub).where(User.role == "admin", User.is_active == True)  # noqa: E712
            ).scalars().all()
        )
    return subs


def _notify_new_email(db: Session, mailbox: str, email_msg) -> None:
    """Create an in-app + push notification for a freshly ingested inbound email."""
    from app.db.models import NotificationType
    from app.services.notifications import create_notification_sync

    sender = email_msg.from_address or "—"
    subject = (email_msg.subject or "(без темы)").strip()
    title = f"Новое письмо · {mailbox}"
    body = f"{sender}: {subject}"[:480]
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
                action_url="/email",
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
                thread.last_message_at = datetime.now(timezone.utc)
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
            existing_thread.last_message_at = datetime.now(timezone.utc)
            return existing_thread.id

    # Create new thread
    thread = EmailThread(
        subject=clean_subject or subject,
        mailbox=mailbox,
        message_count=1,
        last_message_at=datetime.now(timezone.utc),
    )
    db.add(thread)
    db.flush()
    return thread.id


def _mailbox_owner_sub(db: Session, mailbox: str) -> str | None:
    """Owner of a personal mailbox (None for shared/company inboxes)."""
    from app.db.models import MailboxConfig

    row = db.execute(
        select(MailboxConfig.mailbox_type, MailboxConfig.owner_sub)
        .where(MailboxConfig.name == mailbox)
    ).first()
    return row[1] if row and row[0] == "personal" else None


def _store_attachment(
    db: Session,
    att,
    email_message_id: uuid.UUID,
    mailbox: str,
    owner_sub: str | None = None,
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
    )
    db.add(email_att)
    try:
        from app.storage import upload_file as _upload_att
        _upload_att(att.content, storage_path, att.content_type)
    except Exception as e:  # noqa: BLE001
        logger.warning("minio_attachment_upload_failed", error=str(e))

    # Dedup check. Deliberately scoped: a private attachment must never be
    # de-duplicated into a shared document (that would hand a colleague's file to
    # everyone), and a shared document must not be narrowed to one owner. Only
    # documents with the same ownership are reused.
    existing = db.execute(
        select(Document).where(
            Document.file_hash == file_hash,
            Document.owner_sub.is_(None) if owner_sub is None else Document.owner_sub == owner_sub,
        )
    ).scalars().first()
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
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not extension:
        return False
    return (
        db.execute(
            select(FileExtensionAllowlist).where(
                FileExtensionAllowlist.extension == extension,
                FileExtensionAllowlist.is_allowed.is_(True),
            )
        ).scalar_one_or_none()
        is not None
    )


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
