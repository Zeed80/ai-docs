"""IMAP client for multi-mailbox email fetching.

Supports multiple mailboxes (procurement, accounting, general),
each with separate credentials and routing rules.
"""

import email
import hashlib
import imaplib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime

import structlog

from app.config import settings

logger = structlog.get_logger()


@dataclass
class MailboxConfig:
    name: str
    host: str
    port: int
    user: str
    password: str
    ssl: bool = True
    folder: str = "INBOX"
    # Routing: what doc types / roles this mailbox serves
    default_doc_type: str | None = None
    assigned_role: str | None = None
    # Personal mailboxes belong to one employee: we must not touch their \Seen
    # flags (the owner reads this mailbox in a normal mail client), so they are
    # polled with BODY.PEEK from a stored UID watermark instead of IMAP UNSEEN.
    mailbox_type: str = "shared"
    owner_sub: str | None = None
    last_seen_uid: int | None = None

    @property
    def is_personal(self) -> bool:
        return self.mailbox_type == "personal"


@dataclass
class ParsedAttachment:
    filename: str
    content: bytes
    content_type: str
    size: int
    sha256: str


@dataclass
class ParsedEmail:
    message_id: str | None
    in_reply_to: str | None
    from_address: str
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    body_text: str
    body_html: str
    sent_at: datetime | None
    has_attachments: bool
    attachments: list[ParsedAttachment] = field(default_factory=list)


def get_mailbox_configs() -> list[MailboxConfig]:
    """Load active mailbox configs from the database."""
    from sqlalchemy import select

    from app.db.models import MailboxConfig as MailboxConfigDB
    from app.db.sync_session import sync_session
    from app.utils.crypto import decrypt_password

    try:
        with sync_session() as db:
            rows = db.execute(
                select(MailboxConfigDB).where(MailboxConfigDB.is_active == True)  # noqa: E712
            ).scalars().all()
            configs = [
                MailboxConfig(
                    name=row.name,
                    host=row.imap_host,
                    port=row.imap_port,
                    user=row.imap_user,
                    password=decrypt_password(row.imap_password_encrypted),
                    ssl=row.imap_ssl,
                    folder=row.imap_folder,
                    default_doc_type=row.default_doc_type,
                    assigned_role=row.assigned_role,
                    mailbox_type=row.mailbox_type or "shared",
                    owner_sub=row.owner_sub,
                    last_seen_uid=row.last_seen_uid,
                )
                for row in rows
            ]
        return configs
    except Exception as e:
        logger.warning("mailbox_configs_load_failed", error=str(e))
        return []


def decode_mime_header(value: str | None) -> str:
    """Decode MIME encoded header value."""
    if not value:
        return ""
    decoded_parts = decode_header(value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return " ".join(result)


def parse_email_message(raw_bytes: bytes) -> ParsedEmail:
    """Parse raw email bytes into structured data."""
    msg = email.message_from_bytes(raw_bytes)

    # Headers
    message_id = msg.get("Message-ID")
    in_reply_to = msg.get("In-Reply-To")
    from_addr = decode_mime_header(msg.get("From", ""))
    to_addrs = [a.strip() for a in decode_mime_header(msg.get("To", "")).split(",") if a.strip()]
    cc_addrs = [a.strip() for a in decode_mime_header(msg.get("Cc", "")).split(",") if a.strip()]
    subject = decode_mime_header(msg.get("Subject", ""))

    # Date
    sent_at = None
    date_str = msg.get("Date")
    if date_str:
        try:
            sent_at = parsedate_to_datetime(date_str)
        except Exception:
            pass

    # Body
    body_text = ""
    body_html = ""
    attachments: list[ParsedAttachment] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in disposition or part.get_filename():
                # Attachment
                payload = part.get_payload(decode=True)
                if payload:
                    filename = decode_mime_header(part.get_filename()) or "attachment"
                    sha256 = hashlib.sha256(payload).hexdigest()
                    attachments.append(ParsedAttachment(
                        filename=filename,
                        content=payload,
                        content_type=content_type,
                        size=len(payload),
                        sha256=sha256,
                    ))
            elif content_type == "text/plain" and not body_text:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body_text = payload.decode(charset, errors="replace")
            elif content_type == "text/html" and not body_html:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body_html = payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            if msg.get_content_type() == "text/html":
                body_html = payload.decode(charset, errors="replace")
            else:
                body_text = payload.decode(charset, errors="replace")

    return ParsedEmail(
        message_id=message_id,
        in_reply_to=in_reply_to,
        from_address=from_addr,
        to_addresses=to_addrs,
        cc_addresses=cc_addrs,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        sent_at=sent_at,
        has_attachments=len(attachments) > 0,
        attachments=attachments,
    )


def _save_last_seen_uid(mailbox_name: str, uid: int) -> None:
    """Persist the UID watermark for a PEEK-polled (personal) mailbox."""
    from sqlalchemy import select

    from app.db.models import MailboxConfig as MailboxConfigDB
    from app.db.sync_session import sync_session

    try:
        with sync_session() as db:
            row = db.execute(
                select(MailboxConfigDB).where(MailboxConfigDB.name == mailbox_name)
            ).scalar_one_or_none()
            if row is not None and (row.last_seen_uid or 0) < uid:
                row.last_seen_uid = uid
                db.commit()
    except Exception as e:  # noqa: BLE001
        # Losing the watermark only means re-reading messages next run (dedup
        # happens downstream by Message-ID/hash) — never fail the fetch for it.
        logger.warning("imap_uid_watermark_save_failed", mailbox=mailbox_name, error=str(e))


def fetch_unseen_from_mailbox(config: MailboxConfig) -> list[ParsedEmail]:
    """Fetch new messages from a mailbox.

    Shared mailboxes: IMAP UNSEEN + mark \\Seen — they are an integration inbox,
    the flag is our processing state and nobody reads them by hand.

    Personal mailboxes: UID > last_seen_uid + BODY.PEEK — a human reads this
    mailbox in their own client, so the agent must leave \\Seen untouched.
    Progress is tracked by the UID watermark instead of the flag.
    """
    logger.info(
        "imap_connecting", mailbox=config.name, host=config.host,
        mode="peek" if config.is_personal else "unseen",
    )

    try:
        if config.ssl:
            conn = imaplib.IMAP4_SSL(config.host, config.port)
        else:
            conn = imaplib.IMAP4(config.host, config.port)

        conn.login(config.user, config.password)
        conn.select(config.folder)

        personal = config.is_personal
        if personal:
            since_uid = int(config.last_seen_uid or 0) + 1
            status, message_ids = conn.uid("search", None, f"UID {since_uid}:*")
        else:
            status, message_ids = conn.search(None, "UNSEEN")

        if status != "OK" or not message_ids or not message_ids[0]:
            logger.info("imap_no_new_messages", mailbox=config.name)
            conn.logout()
            return []

        ids = message_ids[0].split()
        logger.info("imap_found_messages", mailbox=config.name, count=len(ids))

        emails: list[ParsedEmail] = []
        max_uid = int(config.last_seen_uid or 0)
        for msg_id in ids:
            if personal:
                # "UID n:*" always returns at least one message even when n is
                # past the end — skip anything at or below the watermark.
                try:
                    uid_value = int(msg_id)
                except (TypeError, ValueError):
                    continue
                if uid_value <= (config.last_seen_uid or 0):
                    continue
                status, data = conn.uid("fetch", msg_id, "(BODY.PEEK[])")
            else:
                status, data = conn.fetch(msg_id, "(RFC822)")

            if status != "OK" or not data or not data[0]:
                continue

            raw = data[0][1]
            if isinstance(raw, bytes):
                parsed = parse_email_message(raw)
                emails.append(parsed)

                if personal:
                    max_uid = max(max_uid, uid_value)
                else:
                    conn.store(msg_id, "+FLAGS", "\\Seen")

        conn.logout()

        if personal and max_uid > (config.last_seen_uid or 0):
            _save_last_seen_uid(config.name, max_uid)

        logger.info("imap_fetched", mailbox=config.name, count=len(emails))
        return emails

    except Exception as e:
        logger.error("imap_error", mailbox=config.name, error=str(e))
        return []
