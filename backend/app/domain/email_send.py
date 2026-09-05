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

import hashlib
import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DraftAction, EmailThread


def draft_content_digest(draft_data: dict | None) -> str:
    """sha256 over what actually leaves the building: recipients, subject, body
    and attachments.

    An approval that binds only ``draft_id`` approves an identifier, not a
    letter — the content behind that id can be rewritten between the human's
    decision and the send. Everything that changes the message must change this
    digest, and nothing else may (status, risk flags and bookkeeping are
    deliberately excluded, or a risk_check would invalidate its own approval).
    """
    data = draft_data or {}

    def _addrs(key: str) -> list[str]:
        return sorted(a.strip().lower() for a in (data.get(key) or []) if a)

    payload = {
        "to": _addrs("to_addresses"),
        "cc": _addrs("cc_addresses"),
        "bcc": _addrs("bcc_addresses"),
        "subject": (data.get("subject") or "").strip(),
        "body_html": data.get("body_html") or "",
        "body_text": data.get("body_text") or "",
        "attachments": sorted(str(a) for a in (data.get("attachment_ids") or [])),
        "mailbox": data.get("mailbox") or "",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


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
    owner_sub: str | None = None,
) -> DraftAction:
    """Create a DraftAction queued for email.risk_check → email.send.

    ``mailbox`` picks whose SMTP account the eventual send uses (see
    app/tasks/email_sender.py): explicit override, else the given thread's own
    mailbox, else the single SMTP-configured mailbox of the installation.

    Резолв делается ЗДЕСЬ, а не в момент отправки, потому что ящик входит в
    ``content_digest``: пока он подставлялся воркером, человек подтверждал
    письмо, не зная, с какого адреса оно уйдёт, а digest считался по пустому
    значению. Не разрешилось — поле остаётся пустым, и отправка честно упрётся
    в ошибку вместо письма от произвольного имени.

    ``owner_sub`` is the person (or ``rule:<id>`` / ``agent`` pseudo-actor) this
    draft belongs to. It is what app.domain.email_access.may_access_draft reads
    to decide who may later list, edit or send it — every caller must pass it,
    because a draft with neither owner nor mailbox is reachable by nobody.
    """
    mailbox_name = mailbox
    if not mailbox_name and thread_id:
        thread = await db.get(EmailThread, thread_id)
        if thread:
            mailbox_name = thread.mailbox
    if not mailbox_name:
        mailbox_name = await resolve_default_mailbox(db)

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
            "in_reply_to_message_id": str(in_reply_to_message_id)
            if in_reply_to_message_id
            else None,
            "forward_of_message_id": str(forward_of_message_id) if forward_of_message_id else None,
            "attachment_ids": [str(a) for a in (attachment_ids or [])],
            "status": status,
            "risk_flags": [],
            "created_by_sub": owner_sub,
        },
    )
    draft.draft_data["content_digest"] = draft_content_digest(draft.draft_data)
    db.add(draft)
    await db.flush()
    return draft


def resolve_default_mailbox_sync(db) -> str | None:
    """Ящик, из которого отправлять, когда его не указали явно.

    Черновик без ящика не имел SMTP-аккаунта, и отправка сваливалась в
    глобальный .env, которого здесь нет, — письмо уходило в мнимую отправку.
    Правило намеренно осторожное: если SMTP настроен ровно у ОДНОГО активного
    ОБЩЕГО ящика, он и есть ответ; если их несколько — не выбираем, потому что
    отправить от чужого имени хуже, чем не отправить, и вызывающий обязан
    указать ящик сам.

    Личные ящики из автовыбора исключены совсем: «единственный настроенный»
    легко оказывается личным ящиком сотрудника, и тогда автоподстановка
    отправляла бы письма от его имени — решение, которое никто не принимал.
    """
    from sqlalchemy import select as _sel

    from app.db.models import MailboxConfig

    rows = db.execute(_sel(MailboxConfig.name).where(*_default_mailbox_where())).scalars().all()
    return rows[0] if len(rows) == 1 else None


def _default_mailbox_where():
    from app.db.models import MailboxConfig

    return (
        MailboxConfig.is_active == True,  # noqa: E712
        MailboxConfig.smtp_host.isnot(None),
        MailboxConfig.smtp_host != "",
        MailboxConfig.mailbox_type != "personal",
    )


async def resolve_default_mailbox(db) -> str | None:
    """Async-вариант :func:`resolve_default_mailbox_sync`."""
    from sqlalchemy import select as _sel

    from app.db.models import MailboxConfig

    rows = (
        (await db.execute(_sel(MailboxConfig.name).where(*_default_mailbox_where())))
        .scalars()
        .all()
    )
    return rows[0] if len(rows) == 1 else None
