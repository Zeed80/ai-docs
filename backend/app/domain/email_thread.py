"""Threading headers + reflecting an outbound message back into the thread.

Used by both the human send path (api/email.py::compose_and_send) and the agent
send path (tasks/email_sender.py) so a sent reply:
  * carries In-Reply-To / References so it threads in the recipient's client;
  * shows up as an EmailMessage(is_inbound=False, folder="sent") in our own
    thread view and full-text search — closing the "the agent's own outbound
    mail is invisible" gap.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailAttachment, EmailMessage, EmailThread

logger = structlog.get_logger()


def resolve_threading_headers(parent: EmailMessage | None) -> tuple[str | None, str | None]:
    """(In-Reply-To, References) for a reply to ``parent``."""
    if parent is None or not parent.message_id_header:
        return None, None
    in_reply_to = parent.message_id_header
    refs = (parent.references or "").strip()
    references = f"{refs} {in_reply_to}".strip() if refs else in_reply_to
    return in_reply_to, references


def _snippet(text: str | None, html: str | None) -> str:
    src = (text or "").strip()
    if not src and html:
        import re

        src = re.sub(r"<[^>]+>", " ", html)
    return " ".join(src.split())[:300]


async def record_outbound_message(
    db: AsyncSession,
    *,
    mailbox: str,
    draft_data: dict,
    smtp_message_id: str,
    from_address: str,
    sent_at: datetime | None = None,
) -> EmailMessage:
    """Persist a just-sent email as an outbound EmailMessage in its thread."""
    sent_at = sent_at or datetime.now(timezone.utc)
    to_addresses = draft_data.get("to_addresses") or []
    subject = draft_data.get("subject") or "(без темы)"

    thread_id = None
    parent: EmailMessage | None = None
    raw_parent = draft_data.get("in_reply_to_message_id") or draft_data.get("forward_of_message_id")
    if raw_parent:
        parent = await db.get(EmailMessage, uuid.UUID(str(raw_parent)))
        if parent:
            thread_id = parent.thread_id
    if thread_id is None and draft_data.get("thread_id"):
        thread_id = uuid.UUID(str(draft_data["thread_id"]))

    thread = await db.get(EmailThread, thread_id) if thread_id else None
    if thread is None:
        thread = EmailThread(
            subject=subject.removeprefix("Re: ").removeprefix("RE: "),
            mailbox=mailbox,
            message_count=0,
            folder="inbox",
        )
        db.add(thread)
        await db.flush()

    _, references = resolve_threading_headers(parent)
    snippet = _snippet(draft_data.get("body_text"), draft_data.get("body_html"))

    msg = EmailMessage(
        thread_id=thread.id,
        message_id_header=smtp_message_id,
        in_reply_to=parent.message_id_header if parent else None,
        references=references,
        mailbox=mailbox,
        from_address=from_address,
        to_addresses=to_addresses,
        cc_addresses=draft_data.get("cc_addresses") or [],
        subject=subject,
        body_text=draft_data.get("body_text"),
        body_html=draft_data.get("body_html"),
        sent_at=sent_at,
        received_at=sent_at,
        is_inbound=False,
        is_read=True,
        folder="sent",
        snippet=snippet,
    )
    db.add(msg)
    await db.flush()

    # Copy staged attachments onto the sent message.
    att_ids = [uuid.UUID(str(a)) for a in draft_data.get("attachment_ids", [])]
    if att_ids:
        staged = (
            await db.execute(select(EmailAttachment).where(EmailAttachment.id.in_(att_ids)))
        ).scalars().all()
        for a in staged:
            db.add(
                EmailAttachment(
                    message_id=msg.id,
                    filename=a.filename,
                    content_type=a.content_type,
                    size=a.size,
                    storage_path=a.storage_path,
                    sha256=a.sha256,
                    is_inline=a.is_inline,
                    content_id=a.content_id,
                )
            )
        msg.has_attachments = True
        msg.attachment_count = len(staged)

    thread.message_count = (thread.message_count or 0) + 1
    thread.last_message_at = sent_at
    thread.last_snippet = snippet
    if msg.has_attachments:
        thread.has_attachments = True
    await db.flush()
    logger.info("email_outbound_recorded", thread_id=str(thread.id), message_id=smtp_message_id)
    return msg
