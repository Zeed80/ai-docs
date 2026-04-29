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
    EmailMessage,
    EmailThread,
    FileExtensionAllowlist,
    QuarantineEntry,
)
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


def _get_sync_session() -> Session:
    """Get a synchronous DB session for Celery tasks."""
    engine = create_engine(settings.database_url_sync, pool_pre_ping=True)
    return Session(engine)


@celery_app.task(name="app.tasks.ingest.poll_imap_mailbox", bind=True, max_retries=3)
def poll_imap_mailbox(self, mailbox: str) -> dict:
    """Poll a single IMAP mailbox for new messages.

    Mailboxes: procurement, accounting, general.
    Each has separate credentials from settings.
    """
    from app.tasks.imap_client import get_mailbox_configs, fetch_unseen_from_mailbox

    logger.info("imap_poll_start", mailbox=mailbox)

    configs = get_mailbox_configs()
    config = next((c for c in configs if c.name == mailbox), None)
    if not config:
        logger.warning("imap_mailbox_not_configured", mailbox=mailbox)
        return {"mailbox": mailbox, "fetched": 0, "errors": ["Mailbox not configured"]}

    try:
        emails = fetch_unseen_from_mailbox(config)
    except Exception as e:
        logger.error("imap_fetch_failed", mailbox=mailbox, error=str(e))
        self.retry(countdown=60, exc=e)
        return {"mailbox": mailbox, "fetched": 0, "errors": [str(e)]}

    errors: list[str] = []
    created_docs = 0

    with _get_sync_session() as db:
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
                )
                db.add(email_msg)
                db.flush()

                # Process attachments → Documents
                for att in parsed.attachments:
                    doc = _store_attachment(db, att, email_msg.id, mailbox)
                    if doc:
                        created_docs += 1

                db.commit()

            except Exception as e:
                db.rollback()
                logger.error("imap_process_error", error=str(e), subject=parsed.subject)
                errors.append(f"Error processing '{parsed.subject}': {e}")

    logger.info(
        "imap_poll_complete",
        mailbox=mailbox,
        emails=len(emails),
        documents=created_docs,
        errors=len(errors),
    )
    return {"mailbox": mailbox, "fetched": len(emails), "documents": created_docs, "errors": errors}


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


def _store_attachment(
    db: Session,
    att,
    email_message_id: uuid.UUID,
    mailbox: str,
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

    # Dedup check
    existing = db.execute(
        select(Document).where(Document.file_hash == file_hash)
    ).scalar_one_or_none()
    if existing:
        logger.info("attachment_duplicate", filename=att.filename, hash=file_hash)
        # Still link to this email
        link = DocumentLink(
            document_id=existing.id,
            linked_entity_type="email_message",
            linked_entity_id=email_message_id,
            link_type="attachment",
        )
        db.add(link)
        return None

    # Upload to MinIO
    try:
        from app.storage import upload_file
        upload_file(att.content, storage_path, att.content_type)
    except Exception as e:
        logger.warning("minio_attachment_upload_failed", error=str(e))
        # Continue without upload — file can be re-uploaded later

    # Create Document
    doc = Document(
        file_name=att.filename,
        file_hash=file_hash,
        file_size=att.size,
        mime_type=att.content_type,
        storage_path=storage_path,
        source_channel="email",
        source_email_id=email_message_id,
        status=DocumentStatus.ingested if is_allowed else DocumentStatus.suspicious,
    )
    db.add(doc)
    db.flush()

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
