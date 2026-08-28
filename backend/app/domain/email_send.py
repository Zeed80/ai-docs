"""Single place to queue an outbound email for real delivery.

There used to be two parallel draft-email pipelines: this one (DraftAction,
app.tasks.email_sender.send_email_draft) and a second one built on the
``DraftEmail`` model (app/api/draft_email.py, ``/api/draft-emails``). The
second pipeline's approval step never actually dispatched an SMTP send —
procurement's RFQ-to-suppliers and calendar's payment-followup drafts looked
sent (an Approval could even be granted) but the message never left the
building. Fixed 2026-08-25 by routing every caller through this one function
instead; ``DraftEmail`` is no longer written to by any code path.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DraftAction, EmailThread


async def create_reply_draft(
    db: AsyncSession,
    *,
    to_addresses: list[str],
    subject: str,
    body_html: str = "",
    body_text: str | None = None,
    cc_addresses: list[str] | None = None,
    bcc_addresses: list[str] | None = None,
    thread_id: uuid.UUID | None = None,
    supplier_id: uuid.UUID | None = None,
    context: dict | None = None,
    mailbox: str | None = None,
    in_reply_to_message_id: uuid.UUID | None = None,
    forward_of_message_id: uuid.UUID | None = None,
    attachment_ids: list[uuid.UUID] | None = None,
    status: str = "draft",
) -> DraftAction:
    """Create a DraftAction queued for email.risk_check → email.send.

    ``mailbox`` picks whose SMTP account the eventual send uses (see
    app/tasks/email_sender.py): explicit override, else the given thread's own
    mailbox, else the global .env account as a last resort.
    """
    mailbox_name = mailbox
    if not mailbox_name and thread_id:
        thread = await db.get(EmailThread, thread_id)
        if thread:
            mailbox_name = thread.mailbox

    draft = DraftAction(
        action_type="email.send",
        entity_type="email",
        draft_data={
            "to_addresses": to_addresses,
            "cc_addresses": cc_addresses or [],
            "bcc_addresses": bcc_addresses or [],
            "subject": subject,
            "body_html": body_html,
            "body_text": body_text,
            "thread_id": str(thread_id) if thread_id else None,
            "supplier_id": str(supplier_id) if supplier_id else None,
            "context": context,
            "mailbox": mailbox_name,
            "in_reply_to_message_id": str(in_reply_to_message_id) if in_reply_to_message_id else None,
            "forward_of_message_id": str(forward_of_message_id) if forward_of_message_id else None,
            "attachment_ids": [str(a) for a in (attachment_ids or [])],
            "status": status,
            "risk_flags": [],
        },
    )
    db.add(draft)
    await db.flush()
    return draft
