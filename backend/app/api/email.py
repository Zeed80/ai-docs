"""Email API — skills: email.fetch_new, email.read, email.search,
email.draft, email.style_match, email.risk_check, email.send, email.suggest_template"""

import asyncio
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, delete as sa_delete, select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.acting import get_effective_user
from app.auth.models import UserInfo
from app.db.session import get_db
from app.domain.email_access import (
    hidden_mailbox_names,
    mailbox_filter,
    may_read_mailbox,
    may_write_mailbox,
)
from app.db.models import (
    DraftAction,
    EmailAttachment,
    EmailLabel,
    EmailMessage,
    EmailThread,
    EmailThreadLabel,
    MailboxConfig,
    Party,
)
from app.domain.email import (
    AttachmentProcessRequest,
    AttachmentProcessResponse,
    AttachmentRecognizeRequest,
    AttachmentRecognizeResponse,
    AttachmentRecognitionResult,
    AgentComposeRequest,
    BulkActionResult,
    BulkThreadAction,
    ComposeAssistRequest,
    ComposeAssistResponse,
    ComposeSendRequest,
    ContactOut,
    EmailAttachmentOut,
    EmailDraftCreate,
    EmailDraftOut,
    EmailDraftUpdate,
    EmailFetchRequest,
    EmailFetchResponse,
    EmailLabelCreate,
    EmailLabelOut,
    EmailLabelUpdate,
    EmailMessageOut,
    EmailSearchRequest,
    EmailSearchResponse,
    EmailSendResult,
    RiskCheckRequest,
    RiskCheckResponse,
    RiskFlag,
    StyleAnalyzeRequest,
    StyleAnalyzeResponse,
    TemplateSuggestRequest,
    TemplateSuggestResponse,
    EmailTemplate,
    EmailThreadOut,
)
from app.audit.service import log_action, add_timeline_event

router = APIRouter()
logger = structlog.get_logger()


@router.post("/fetch", response_model=EmailFetchResponse)
async def fetch_new_emails(
    payload: EmailFetchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.fetch_new — Check for new emails via IMAP.

    This is a stub — actual IMAP fetching is done by Celery task.
    This endpoint triggers the task and returns results.
    """
    from app.tasks.email_triage import run_triage

    task = run_triage.delay(payload.mailbox)
    logger.info("email_triage_triggered", mailbox=payload.mailbox, task_id=task.id)
    return EmailFetchResponse(
        fetched_count=0,
        new_messages=[],
        errors=[],
        task_id=task.id,
    )


# ── email.search ───────────────────────────────────────────────────────────


@router.post("/search", response_model=EmailSearchResponse)
async def search_emails(
    payload: EmailSearchRequest,
    request: Request,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.search — Full-text search with folder/label/date filters."""
    query = select(EmailMessage).options(selectinload(EmailMessage.attachments))

    scope = await mailbox_filter(
        db, user, mailbox_col=EmailMessage.mailbox, for_agent=request_is_agent(request)
    )
    if scope is not None:
        query = query.where(scope)

    is_pg = db.bind.dialect.name == "postgresql"
    rank = None
    if payload.query:
        if is_pg:
            tsv = func.to_tsvector(
                "russian",
                func.coalesce(EmailMessage.subject, "")
                + " "
                + func.coalesce(EmailMessage.body_text, ""),
            )
            tsq = func.websearch_to_tsquery("russian", payload.query)
            query = query.where(
                or_(tsv.op("@@")(tsq), EmailMessage.from_address.ilike(f"%{payload.query}%"))
            )
            rank = func.ts_rank(tsv, tsq)
        else:
            like = f"%{payload.query}%"
            query = query.where(
                or_(
                    EmailMessage.subject.ilike(like),
                    EmailMessage.body_text.ilike(like),
                    EmailMessage.from_address.ilike(like),
                )
            )
    if payload.email_address:
        query = query.where(EmailMessage.from_address.ilike(f"%{payload.email_address}%"))
    if payload.from_addr:
        query = query.where(EmailMessage.from_address.ilike(f"%{payload.from_addr}%"))
    if payload.to_addr:
        query = query.where(cast(EmailMessage.to_addresses, String).ilike(f"%{payload.to_addr}%"))
    if payload.mailbox:
        query = query.where(EmailMessage.mailbox == payload.mailbox)
    if payload.folder:
        query = query.where(EmailMessage.folder == payload.folder)
    if payload.is_unread is True:
        query = query.where(EmailMessage.is_read == False)  # noqa: E712
    if payload.is_starred is not None:
        query = query.where(EmailMessage.is_starred == payload.is_starred)
    if payload.has_attachments is not None:
        query = query.where(EmailMessage.has_attachments == payload.has_attachments)
    if payload.date_from:
        query = query.where(EmailMessage.received_at >= payload.date_from)
    if payload.date_to:
        query = query.where(EmailMessage.received_at <= payload.date_to)
    if payload.label_ids:
        query = query.where(
            EmailMessage.thread_id.in_(
                select(EmailThreadLabel.thread_id).where(
                    EmailThreadLabel.label_id.in_(payload.label_ids)
                )
            )
        )

    if payload.supplier_id:
        party = (
            await db.execute(select(Party).where(Party.id == payload.supplier_id))
        ).scalar_one_or_none()
        if party and party.contact_email:
            query = query.where(EmailMessage.from_address.ilike(f"%{party.contact_email}%"))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0

    if payload.sort == "relevance" and rank is not None:
        query = query.order_by(rank.desc(), EmailMessage.received_at.desc())
    elif payload.sort == "date_asc":
        query = query.order_by(EmailMessage.received_at.asc())
    else:
        query = query.order_by(EmailMessage.received_at.desc().nullslast())

    result = await db.execute(query.limit(min(payload.limit, 200)))
    messages = result.scalars().all()
    return EmailSearchResponse(results=messages, total=total)


# ── email.mailboxes (client sidebar) ───────────────────────────────────────


def request_is_agent(request: Request) -> bool:
    """Heuristic: this call arrived through the agent capability router.

    The router relays ``X-Acting-User`` verbatim and authenticates with the
    service key; a human browser session sends neither. Used to apply the
    ``sweep_enabled`` consent gate on personal mailboxes for agent reads.
    """
    return bool((request.headers.get("x-acting-user") or "").strip())


class EmailMailboxChip(BaseModel):
    name: str
    display_name: str | None = None
    is_personal: bool = False
    thread_count: int = 0
    message_count: int = 0
    unread_count: int = 0
    last_sync_at: datetime | None = None
    sync_error: str | None = None


@router.get("/mailboxes", response_model=list[EmailMailboxChip])
async def list_email_mailboxes(
    request: Request,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> list[EmailMailboxChip]:
    """Skill: email.mailboxes — Mailboxes visible to the caller, with counts and
    sync status, for the client sidebar / filter chips."""
    for_agent = request_is_agent(request)
    hidden = set(await hidden_mailbox_names(db, user, for_agent=for_agent))

    cfgs = (
        await db.execute(select(MailboxConfig).where(MailboxConfig.is_active == True))  # noqa: E712
    ).scalars().all()
    cfgs = [c for c in cfgs if c.name not in hidden]
    names = [c.name for c in cfgs]

    msg_rows = (
        await db.execute(
            select(EmailMessage.mailbox, func.count(EmailMessage.id))
            .where(EmailMessage.mailbox.in_(names))
            .group_by(EmailMessage.mailbox)
        )
    ).all() if names else []
    thread_rows = (
        await db.execute(
            select(EmailThread.mailbox, func.count(EmailThread.id))
            .where(EmailThread.mailbox.in_(names))
            .group_by(EmailThread.mailbox)
        )
    ).all() if names else []
    unread_rows = (
        await db.execute(
            select(EmailThread.mailbox, func.count(EmailThread.id))
            .where(
                EmailThread.mailbox.in_(names),
                EmailThread.is_read == False,  # noqa: E712
                EmailThread.folder == "inbox",
            )
            .group_by(EmailThread.mailbox)
        )
    ).all() if names else []
    msg_counts = {m: int(n) for m, n in msg_rows}
    thread_counts = {m: int(n) for m, n in thread_rows}
    unread_counts = {m: int(n) for m, n in unread_rows}

    return [
        EmailMailboxChip(
            name=c.name,
            display_name=c.display_name,
            is_personal=c.mailbox_type == "personal",
            thread_count=thread_counts.get(c.name, 0),
            message_count=msg_counts.get(c.name, 0),
            unread_count=unread_counts.get(c.name, 0),
            last_sync_at=c.last_sync_at,
            sync_error=c.sync_error,
        )
        for c in cfgs
    ]


# ── Thread viewer ──────────────────────────────────────────────────────────

_SYSTEM_FOLDERS = ("inbox", "sent", "drafts", "archive", "trash", "spam")


def _thread_out(thread: EmailThread, *, with_messages: bool = False) -> EmailThreadOut:
    msgs = sorted(
        thread.messages or [],
        key=lambda m: m.received_at or m.sent_at or m.created_at,
    )
    inbound = [m for m in msgs if m.is_inbound]
    sender = (inbound[-1].from_address if inbound else (msgs[-1].from_address if msgs else None))
    return EmailThreadOut(
        id=thread.id,
        subject=thread.subject,
        mailbox=thread.mailbox,
        party_id=thread.party_id,
        message_count=thread.message_count,
        last_message_at=thread.last_message_at,
        created_at=thread.created_at,
        is_read=thread.is_read,
        is_starred=thread.is_starred,
        has_attachments=thread.has_attachments,
        folder=thread.folder,
        last_snippet=thread.last_snippet,
        unread_count=thread.unread_count,
        sender=sender,
        labels=[EmailLabelOut.model_validate(l) for l in (thread.labels or [])],
        messages=[EmailMessageOut.model_validate(m) for m in msgs] if with_messages else [],
    )


async def _thread_label_counts(db: AsyncSession, label_ids: list[uuid.UUID]) -> dict:
    if not label_ids:
        return {}
    rows = (
        await db.execute(
            select(EmailThreadLabel.label_id, func.count())
            .where(EmailThreadLabel.label_id.in_(label_ids))
            .group_by(EmailThreadLabel.label_id)
        )
    ).all()
    return {lid: int(n) for lid, n in rows}


@router.get("/threads", response_model=list[EmailThreadOut])
async def list_threads(
    request: Request,
    mailbox: str | None = None,
    folder: str | None = None,
    label_id: uuid.UUID | None = None,
    is_unread: bool | None = None,
    is_starred: bool | None = None,
    has_attachments: bool | None = None,
    limit: int = 50,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.list_threads — List email threads (with client filters)."""
    for_agent = request_is_agent(request)
    query = (
        select(EmailThread)
        .options(
            selectinload(EmailThread.messages),
            selectinload(EmailThread.labels),
        )
    )
    if mailbox:
        if not await may_read_mailbox(db, user, mailbox, for_agent=for_agent):
            raise HTTPException(status_code=403, detail="Личный почтовый ящик другого пользователя")
        query = query.where(EmailThread.mailbox == mailbox)
    else:
        scope = await mailbox_filter(db, user, mailbox_col=EmailThread.mailbox, for_agent=for_agent)
        if scope is not None:
            query = query.where(scope)

    # Default thread view = the inbox; other folders are opt-in via ?folder=.
    query = query.where(EmailThread.folder == (folder or "inbox"))
    if label_id is not None:
        query = query.where(
            EmailThread.id.in_(
                select(EmailThreadLabel.thread_id).where(EmailThreadLabel.label_id == label_id)
            )
        )
    if is_unread is True:
        query = query.where(EmailThread.is_read == False)  # noqa: E712
    if is_starred is not None:
        query = query.where(EmailThread.is_starred == is_starred)
    if has_attachments is not None:
        query = query.where(EmailThread.has_attachments == has_attachments)

    query = query.order_by(EmailThread.last_message_at.desc().nullslast()).limit(min(limit, 200))
    result = await db.execute(query)
    return [_thread_out(t) for t in result.scalars().all()]


@router.get("/threads/{thread_id}", response_model=EmailThreadOut)
async def get_thread(
    thread_id: uuid.UUID,
    request: Request,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.get_thread — Get thread with all messages + attachments."""
    result = await db.execute(
        select(EmailThread)
        .where(EmailThread.id == thread_id)
        .options(
            selectinload(EmailThread.messages).selectinload(EmailMessage.attachments),
            selectinload(EmailThread.labels),
        )
    )
    thread = result.scalar_one_or_none()
    if not thread:
        raise HTTPException(404, "Thread not found")
    # 404 (not 403) for someone else's personal mailbox: the existence of a
    # colleague's thread is itself private.
    if not await may_read_mailbox(db, user, thread.mailbox, for_agent=request_is_agent(request)):
        raise HTTPException(404, "Thread not found")
    return _thread_out(thread, with_messages=True)


@router.post("/threads/actions", response_model=BulkActionResult)
async def bulk_thread_action(
    payload: BulkThreadAction,
    request: Request,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.label — Bulk mark read/star/move/label email threads."""
    for_agent = request_is_agent(request)
    hidden = set(await hidden_mailbox_names(db, user, for_agent=for_agent))
    threads = (
        await db.execute(
            select(EmailThread)
            .where(EmailThread.id.in_(payload.thread_ids))
            .options(selectinload(EmailThread.messages))
        )
    ).scalars().all()
    threads = [t for t in threads if t.mailbox not in hidden]

    actor = "sveta" if for_agent else "user"
    updated = 0
    for t in threads:
        if payload.action in ("read", "unread"):
            val = payload.action == "read"
            t.is_read = val
            t.unread_count = 0 if val else max(t.message_count, 1)
            for m in t.messages or []:
                m.is_read = val
        elif payload.action in ("star", "unstar"):
            t.is_starred = payload.action == "star"
        elif payload.action in ("archive", "trash", "spam", "inbox"):
            t.folder = "archive" if payload.action == "archive" else (
                "inbox" if payload.action == "inbox" else payload.action
            )
            for m in t.messages or []:
                m.folder = t.folder
        elif payload.action == "move" and payload.folder:
            t.folder = payload.folder
            for m in t.messages or []:
                m.folder = payload.folder
        elif payload.action == "add_label" and payload.label_id:
            exists = await db.get(EmailThreadLabel, (t.id, payload.label_id))
            if not exists:
                db.add(EmailThreadLabel(thread_id=t.id, label_id=payload.label_id, added_by=actor))
        elif payload.action == "remove_label" and payload.label_id:
            await db.execute(
                sa_delete(EmailThreadLabel).where(
                    EmailThreadLabel.thread_id == t.id,
                    EmailThreadLabel.label_id == payload.label_id,
                )
            )
        else:
            continue
        updated += 1

    await db.commit()
    try:
        from app.core.chat_bus import chat_bus

        for t in threads:
            await chat_bus.publish({"type": "email.thread_updated", "thread_id": str(t.id),
                                    "mailbox": t.mailbox})
    except Exception:  # noqa: BLE001
        pass
    logger.info("email_bulk_thread_action", action=payload.action, updated=updated, actor=actor)
    return BulkActionResult(updated=updated)


# ── Labels ─────────────────────────────────────────────────────────────────


@router.get("/labels", response_model=list[EmailLabelOut])
async def list_labels(
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.labels_list — List email labels visible to the caller."""
    rows = (
        await db.execute(
            select(EmailLabel).where(
                or_(EmailLabel.owner_sub.is_(None), EmailLabel.owner_sub == user.sub)
            ).order_by(EmailLabel.is_system.desc(), EmailLabel.name)
        )
    ).scalars().all()
    counts = await _thread_label_counts(db, [l.id for l in rows])
    out = []
    for l in rows:
        o = EmailLabelOut.model_validate(l)
        o.thread_count = counts.get(l.id, 0)
        out.append(o)
    return out


@router.post("/labels", response_model=EmailLabelOut, status_code=201)
async def create_label(
    payload: EmailLabelCreate,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.labels_create — Create an email label."""
    label = EmailLabel(
        name=payload.name, color=payload.color, mailbox=payload.mailbox,
        owner_sub=user.sub, is_system=False,
    )
    db.add(label)
    await db.commit()
    await db.refresh(label)
    return EmailLabelOut.model_validate(label)


@router.patch("/labels/{label_id}", response_model=EmailLabelOut)
async def update_label(
    label_id: uuid.UUID,
    payload: EmailLabelUpdate,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    label = await db.get(EmailLabel, label_id)
    if not label or label.is_system:
        raise HTTPException(404, "Label not found")
    if label.owner_sub not in (None, user.sub):
        raise HTTPException(403, "Not your label")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(label, k, v)
    await db.commit()
    await db.refresh(label)
    return EmailLabelOut.model_validate(label)


@router.delete("/labels/{label_id}", status_code=204)
async def delete_label(
    label_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    label = await db.get(EmailLabel, label_id)
    if not label or label.is_system:
        raise HTTPException(404, "Label not found")
    if label.owner_sub not in (None, user.sub):
        raise HTTPException(403, "Not your label")
    await db.execute(sa_delete(EmailThreadLabel).where(EmailThreadLabel.label_id == label_id))
    await db.delete(label)
    await db.commit()


# ── Contacts autocomplete ──────────────────────────────────────────────────


@router.get("/contacts", response_model=list[ContactOut])
async def list_contacts(
    q: str = "",
    limit: int = 10,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.contacts — Address autocomplete from suppliers + history."""
    q = (q or "").strip()
    seen: dict[str, ContactOut] = {}
    if q:
        parties = (
            await db.execute(
                select(Party).where(
                    Party.contact_email.isnot(None),
                    or_(Party.contact_email.ilike(f"%{q}%"), Party.name.ilike(f"%{q}%")),
                ).limit(limit)
            )
        ).scalars().all()
        for p in parties:
            if p.contact_email:
                seen[p.contact_email.lower()] = ContactOut(email=p.contact_email, name=p.name)
    if len(seen) < limit:
        scope = await mailbox_filter(db, user, mailbox_col=EmailMessage.mailbox)
        mq = select(EmailMessage.from_address).where(EmailMessage.is_inbound == True)  # noqa: E712
        if scope is not None:
            mq = mq.where(scope)
        if q:
            mq = mq.where(EmailMessage.from_address.ilike(f"%{q}%"))
        for (addr,) in (await db.execute(mq.order_by(EmailMessage.received_at.desc()).limit(50))).all():
            key = (addr or "").lower()
            if key and key not in seen:
                seen[key] = ContactOut(email=addr)
            if len(seen) >= limit:
                break
    return list(seen.values())[:limit]


# ── Compose attachments ────────────────────────────────────────────────────


@router.post("/attachments/upload", response_model=EmailAttachmentOut)
async def upload_compose_attachment(
    file: UploadFile = File(...),
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.attachment_upload — Stage a file for an outbound email."""
    import hashlib

    content = await file.read()
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(413, "Вложение больше 25 МБ")
    sha = hashlib.sha256(content).hexdigest()
    storage_path = f"email-attachments/{sha[:2]}/{sha}"
    try:
        from app.storage import upload_file

        await asyncio.to_thread(
            upload_file, content, storage_path, file.content_type or "application/octet-stream"
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Хранилище недоступно: {exc}")
    att = EmailAttachment(
        message_id=None,
        filename=file.filename or "attachment",
        content_type=file.content_type,
        size=len(content),
        storage_path=storage_path,
        sha256=sha,
    )
    db.add(att)
    await db.commit()
    await db.refresh(att)
    return EmailAttachmentOut.model_validate(att)


# ── email.draft ────────────────────────────────────────────────────────────


@router.post("/drafts", response_model=EmailDraftOut)
async def create_draft(
    payload: EmailDraftCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.draft — Create email draft."""
    from app.domain.email_send import create_reply_draft

    draft = await create_reply_draft(
        db,
        to_addresses=payload.to_addresses,
        cc_addresses=payload.cc_addresses,
        bcc_addresses=payload.bcc_addresses,
        subject=payload.subject,
        body_html=payload.body_html,
        body_text=payload.body_text,
        thread_id=payload.thread_id,
        supplier_id=payload.supplier_id,
        context=payload.context,
        mailbox=payload.mailbox,
        in_reply_to_message_id=payload.in_reply_to_message_id,
        forward_of_message_id=payload.forward_of_message_id,
        attachment_ids=payload.attachment_ids,
    )
    await db.commit()
    await db.refresh(draft)
    logger.info("email_draft_created", draft_id=str(draft.id))
    return _draft_to_out(draft)


@router.patch("/drafts/{draft_id}", response_model=EmailDraftOut)
async def update_draft(
    draft_id: uuid.UUID,
    payload: EmailDraftUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.draft_update — Autosave / edit a draft in place."""
    from sqlalchemy.orm.attributes import flag_modified

    draft = await db.get(DraftAction, draft_id)
    if not draft or draft.action_type != "email.send":
        raise HTTPException(404, "Draft not found")
    if draft.executed:
        raise HTTPException(400, "Письмо уже отправлено")
    data = dict(draft.draft_data or {})
    patch = payload.model_dump(exclude_none=True)
    if "attachment_ids" in patch:
        patch["attachment_ids"] = [str(a) for a in patch["attachment_ids"]]
    data.update(patch)
    data["status"] = "draft"  # any edit re-opens risk check
    draft.draft_data = data
    flag_modified(draft, "draft_data")
    await db.commit()
    await db.refresh(draft)
    return _draft_to_out(draft)


@router.get("/drafts", response_model=list[EmailDraftOut])
async def list_drafts(
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.list_drafts — List email drafts."""
    result = await db.execute(
        select(DraftAction)
        .where(DraftAction.action_type == "email.send", DraftAction.executed == False)
        .order_by(DraftAction.created_at.desc())
    )
    drafts = result.scalars().all()
    return [_draft_to_out(d) for d in drafts]


@router.get("/drafts/{draft_id}", response_model=EmailDraftOut)
async def get_draft(
    draft_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get email draft."""
    result = await db.execute(
        select(DraftAction).where(DraftAction.id == draft_id)
    )
    draft = result.scalar_one_or_none()
    if not draft:
        raise HTTPException(404, "Draft not found")
    return _draft_to_out(draft)


def _draft_to_out(draft: DraftAction) -> EmailDraftOut:
    data = draft.draft_data or {}
    return EmailDraftOut(
        id=draft.id,
        to_addresses=data.get("to_addresses", []),
        cc_addresses=data.get("cc_addresses"),
        bcc_addresses=data.get("bcc_addresses"),
        subject=data.get("subject", ""),
        body_html=data.get("body_html"),
        body_text=data.get("body_text"),
        thread_id=uuid.UUID(data["thread_id"]) if data.get("thread_id") else None,
        mailbox=data.get("mailbox"),
        status=data.get("status", "draft"),
        risk_flags=data.get("risk_flags", []),
        attachment_ids=[uuid.UUID(a) for a in data.get("attachment_ids", [])],
        created_at=draft.created_at,
    )


# ── Direct human send (no agent gate) ──────────────────────────────────────


@router.post("/send", response_model=EmailSendResult)
async def compose_and_send(
    payload: ComposeSendRequest,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Send an email composed by a human. Not the agent gate — a person
    hitting Send is the authorisation. Personal mailboxes: owner only."""
    from app.domain.email_send import create_reply_draft
    from sqlalchemy.orm.attributes import flag_modified

    if not await may_write_mailbox(db, user, payload.mailbox):
        raise HTTPException(403, "Нет доступа для отправки из этого ящика")

    thread_id = None
    if payload.in_reply_to_message_id:
        parent = await db.get(EmailMessage, payload.in_reply_to_message_id)
        if parent:
            thread_id = parent.thread_id

    if payload.draft_id:
        draft = await db.get(DraftAction, payload.draft_id)
        if not draft or draft.action_type != "email.send":
            raise HTTPException(404, "Draft not found")
        data = dict(draft.draft_data or {})
        data.update(
            to_addresses=payload.to_addresses,
            cc_addresses=payload.cc_addresses,
            bcc_addresses=payload.bcc_addresses,
            subject=payload.subject,
            body_html=payload.body_html,
            body_text=payload.body_text,
            mailbox=payload.mailbox,
            attachment_ids=[str(a) for a in payload.attachment_ids],
            in_reply_to_message_id=str(payload.in_reply_to_message_id) if payload.in_reply_to_message_id else None,
            status="approved",
            sent_by="user",
        )
        draft.draft_data = data
        flag_modified(draft, "draft_data")
    else:
        draft = await create_reply_draft(
            db,
            to_addresses=payload.to_addresses,
            cc_addresses=payload.cc_addresses,
            bcc_addresses=payload.bcc_addresses,
            subject=payload.subject,
            body_html=payload.body_html,
            body_text=payload.body_text,
            thread_id=thread_id,
            mailbox=payload.mailbox,
            in_reply_to_message_id=payload.in_reply_to_message_id,
            forward_of_message_id=payload.forward_of_message_id,
            attachment_ids=payload.attachment_ids,
            status="approved",
        )
        d = dict(draft.draft_data or {})
        d["sent_by"] = "user"
        draft.draft_data = d
        flag_modified(draft, "draft_data")

    await db.commit()
    await db.refresh(draft)

    from app.tasks.email_sender import send_email_draft

    task = send_email_draft.delay(str(draft.id))
    # Do NOT set executed here — the Celery task sets it after the SMTP send
    # actually succeeds; setting it now makes the task bail with "already_sent".
    d = dict(draft.draft_data or {})
    d["status"] = "queued"
    d["task_id"] = task.id
    draft.draft_data = d
    flag_modified(draft, "draft_data")
    await db.commit()

    await log_action(
        db, action="email.send", entity_type="email", entity_id=draft.id,
        details={"to": payload.to_addresses, "subject": payload.subject, "by": user.sub},
    )
    logger.info("email_user_send_queued", draft_id=str(draft.id), user=user.sub)
    return EmailSendResult(task_id=task.id, draft_id=draft.id, status="queued")


# ── email.style_match ─────────────────────────────────────────────────────


STYLE_SYSTEM = """You are a communication style analyzer for business emails.
Analyze the writing style of emails and provide recommendations. Respond in JSON only."""

STYLE_PROMPT = """Analyze the writing style of these {count} emails:

{emails_text}

Respond with JSON:
{{
  "tone": "formal|friendly|neutral",
  "language": "ru|en|mixed",
  "greeting_style": "<typical greeting>",
  "closing_style": "<typical closing>",
  "avg_length": <average word count>,
  "recommendations": ["<recommendation 1>", "<recommendation 2>"]
}}"""


@router.post("/style-analyze", response_model=StyleAnalyzeResponse)
async def analyze_style(
    payload: StyleAnalyzeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.style_match — Analyze communication style with a counterparty."""
    query = select(EmailMessage).order_by(EmailMessage.received_at.desc())

    if payload.email_address:
        query = query.where(
            EmailMessage.from_address.ilike(f"%{payload.email_address}%")
        )
    elif payload.supplier_id:
        party_result = await db.execute(
            select(Party).where(Party.id == payload.supplier_id)
        )
        party = party_result.scalar_one_or_none()
        if party and party.contact_email:
            query = query.where(
                EmailMessage.from_address.ilike(f"%{party.contact_email}%")
            )

    query = query.limit(payload.sample_count)
    result = await db.execute(query)
    messages = result.scalars().all()

    if not messages:
        return StyleAnalyzeResponse(
            tone="neutral", language="ru", sample_count=0,
            recommendations=["Нет предыдущей переписки для анализа"],
        )

    # Build text for analysis
    emails_text = "\n---\n".join(
        f"From: {m.from_address}\nSubject: {m.subject}\n{(m.body_text or '')[:500]}"
        for m in messages
    )

    try:
        from app.ai.router import ai_router

        ai_result = await ai_router.analyze_email_style(emails_text, len(messages))

        return StyleAnalyzeResponse(
            tone=ai_result.get("tone", "neutral"),
            language=ai_result.get("language", "ru"),
            greeting_style=ai_result.get("greeting_style"),
            closing_style=ai_result.get("closing_style"),
            avg_length=ai_result.get("avg_length", 0),
            recommendations=ai_result.get("recommendations", []),
            sample_count=len(messages),
        )
    except Exception as e:
        logger.warning("style_analyze_failed", error=str(e))
        return StyleAnalyzeResponse(
            tone="neutral", language="ru", sample_count=len(messages),
            recommendations=["Автоанализ недоступен, используйте нейтральный тон"],
        )


# ── email.compose (AI help + agent generation) ────────────────────────────


class ComposeAssistStart(BaseModel):
    task_id: str


class ComposeAssistPoll(BaseModel):
    status: str  # pending | done | error
    result: ComposeAssistResponse | None = None
    error: str | None = None


@router.post("/compose/assist", response_model=ComposeAssistStart)
async def compose_assist(
    payload: ComposeAssistRequest,
    user: UserInfo = Depends(get_effective_user),
):
    """Skill: email.compose_assist — start an agentic "improve this draft" turn.

    Runs as a real headless agent turn (looks things up with tools — invoice
    line items, stock, supplier data) on the model configured for email
    drafting. Async because that can take minutes; poll GET
    /compose/assist/{task_id}. Not gated — nothing is sent.
    """
    from app.tasks.email_compose_task import compose_assist_task

    task = compose_assist_task.delay({
        "subject": payload.subject,
        "body": payload.body,
        "instruction": payload.instruction,
        "thread_id": str(payload.thread_id) if payload.thread_id else None,
        "supplier_id": str(payload.supplier_id) if payload.supplier_id else None,
        "invoice_id": str(payload.invoice_id) if payload.invoice_id else None,
        "mailbox": payload.mailbox,
        "acting_user_sub": user.sub,
    })
    return ComposeAssistStart(task_id=task.id)


@router.get("/compose/assist/{task_id}", response_model=ComposeAssistPoll)
async def compose_assist_poll(task_id: str):
    """Poll an email.compose_assist turn."""
    from celery.result import AsyncResult

    from app.tasks.celery_app import celery_app

    r = AsyncResult(task_id, app=celery_app)
    if not r.ready():
        return ComposeAssistPoll(status="pending")
    try:
        data = r.get(timeout=1)
    except Exception as exc:  # noqa: BLE001
        return ComposeAssistPoll(status="error", error=str(exc))
    if isinstance(data, dict) and data.get("error"):
        return ComposeAssistPoll(status="error", error=data["error"])
    return ComposeAssistPoll(
        status="done",
        result=ComposeAssistResponse(
            subject=data.get("subject", ""),
            body_html=data.get("body_html", ""),
            body_text=data.get("body_text", ""),
            diff=data.get("diff", []),
            notes=data.get("notes", []),
            tone=data.get("tone", "formal"),
        ),
    )


@router.post("/compose/generate", response_model=EmailDraftOut)
async def agent_generate_draft(
    payload: AgentComposeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.compose — Generate a draft from an intent + context.

    Creates a DraftAction (not sent). The agent then follows the existing
    risk_check -> send [GATE] path.
    """
    from app.domain.email_compose import ComposeContext, generate_draft_body
    from app.domain.email_send import create_reply_draft

    res = await generate_draft_body(
        db,
        intent=payload.intent,
        context=ComposeContext(
            thread_id=payload.thread_id,
            supplier_id=payload.supplier_id,
            invoice_id=payload.invoice_id,
            mailbox=payload.mailbox,
        ),
        tone_override=payload.tone,
        acting_user_sub=(request.headers.get("x-acting-user") or "").strip() or None,
    )
    draft = await create_reply_draft(
        db,
        to_addresses=payload.to_addresses,
        subject=res.subject,
        body_html=res.body_html,
        body_text=res.body_text,
        thread_id=payload.thread_id,
        supplier_id=payload.supplier_id,
        mailbox=payload.mailbox,
        attachment_ids=payload.attachment_ids,
    )
    await db.commit()
    await db.refresh(draft)
    logger.info("email_agent_draft_generated", draft_id=str(draft.id))
    return _draft_to_out(draft)


# ── email.risk_check ───────────────────────────────────────────────────────


@router.post("/drafts/{draft_id}/risk-check", response_model=RiskCheckResponse)
async def risk_check(
    draft_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.risk_check — Check email draft for risks before sending."""
    result = await db.execute(
        select(DraftAction).where(DraftAction.id == draft_id)
    )
    draft = result.scalar_one_or_none()
    if not draft:
        raise HTTPException(404, "Draft not found")

    data = draft.draft_data or {}
    flags: list[RiskFlag] = []

    # Detector 1: External domain (our own mail domain + known supplier domains)
    to_addrs = data.get("to_addresses", [])
    from app.domain.email_rules import known_domains as _known_domains

    known = await _known_domains(db)
    for addr in to_addrs:
        domain = addr.split("@")[-1].lower() if "@" in addr else ""
        # When nothing is configured yet (empty `known`) we cannot confirm a
        # domain is internal — flag it (warning, overridable) rather than stay
        # silent. Once mailboxes/suppliers exist the check becomes precise.
        if domain and domain not in known:
            flags.append(RiskFlag(
                code="external_domain",
                severity="warning",
                message=f"Внешний домен получателя: {domain}",
            ))
            break

    # Detector 2: Amount mentioned without attachment context
    body = (data.get("body_text") or data.get("body_html") or "").lower()
    amount_words = ["оплат", "сумм", "счёт на", "перевод", "р.", "руб"]
    if any(w in body for w in amount_words):
        context = data.get("context") or {}
        if not context.get("invoice_id") and not context.get("document_id"):
            flags.append(RiskFlag(
                code="amount_no_attachment",
                severity="warning",
                message="Упомянута сумма/оплата, но нет привязки к документу",
            ))

    # Detector 3: Recipient not in supplier card
    supplier_id = data.get("supplier_id")
    if supplier_id:
        party_result = await db.execute(
            select(Party).where(Party.id == uuid.UUID(supplier_id))
        )
        party = party_result.scalar_one_or_none()
        if party and party.contact_email:
            if not any(party.contact_email.lower() in addr.lower() for addr in to_addrs):
                flags.append(RiskFlag(
                    code="recipient_mismatch",
                    severity="warning",
                    message=f"Получатель не совпадает с email поставщика ({party.contact_email})",
                ))

    # Detector 4: Language mismatch (Russian body sent to non-RU domain)
    has_cyrillic = any(ord(c) > 127 for c in body[:100])
    for addr in to_addrs:
        domain = addr.split("@")[-1] if "@" in addr else ""
        if has_cyrillic and domain and not domain.endswith((".ru", ".рф", ".su")):
            flags.append(RiskFlag(
                code="language_mismatch",
                severity="warning",
                message=f"Русский текст отправляется на домен {domain}",
                can_override=True,
            ))
            break

    # Detector 5: Sensitive keywords
    sensitive_words = ["конфиденциальн", "секрет", "не для распростран", "внутренн"]
    for word in sensitive_words:
        if word in body:
            flags.append(RiskFlag(
                code="sensitive_content",
                severity="error",
                message=f"Обнаружено чувствительное содержание: «{word}...»",
                can_override=True,
            ))
            break

    is_safe = not any(f.severity == "error" for f in flags)

    # Update draft status
    from sqlalchemy.orm.attributes import flag_modified
    data["status"] = "risk_checked"
    data["risk_flags"] = [f.model_dump() for f in flags]
    draft.draft_data = data
    flag_modified(draft, "draft_data")
    await db.commit()

    return RiskCheckResponse(draft_id=draft.id, is_safe=is_safe, flags=flags)


# ── email.send ─────────────────────────────────────────────────────────────


@router.post("/drafts/{draft_id}/send")
async def send_email(
    draft_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.send — Send email draft via SMTP (approval gate)."""
    result = await db.execute(
        select(DraftAction).where(DraftAction.id == draft_id)
    )
    draft = result.scalar_one_or_none()
    if not draft:
        raise HTTPException(404, "Draft not found")

    if draft.executed:
        raise HTTPException(400, "Email already sent")

    data = draft.draft_data or {}

    # Check risk_check was done
    if data.get("status") not in ("risk_checked", "approved"):
        raise HTTPException(400, "Risk check required before sending")

    # Check for blocking risks
    risk_flags = data.get("risk_flags", [])
    blocking = [f for f in risk_flags if f.get("severity") == "error" and f.get("can_override") is False]
    if blocking:
        raise HTTPException(400, f"Blocked by risk: {blocking[0]['message']}")

    # Dispatch SMTP sending to Celery (non-blocking)
    try:
        from app.tasks.email_sender import send_email_draft
        task = send_email_draft.delay(str(draft_id))
        # Idempotency is enforced by the 400 above (draft.executed) and by the
        # task itself; do NOT pre-set executed here or the task returns
        # "already_sent" without sending anything.
        from sqlalchemy.orm.attributes import flag_modified
        draft.draft_data = {**(data), "status": "queued", "task_id": task.id}
        flag_modified(draft, "draft_data")
        await db.commit()
        logger.info("email_send_queued", draft_id=str(draft_id), task_id=task.id)
        return {"status": "queued", "draft_id": str(draft_id), "task_id": task.id}
    except Exception as exc:
        logger.error("email_send_dispatch_failed", draft_id=str(draft_id), error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to queue email sending")


# ── email.suggest_template ─────────────────────────────────────────────────


TEMPLATES = {
    "payment_reminder": EmailTemplate(
        name="Напоминание об оплате",
        subject="Напоминание об оплате счёта №{invoice_number}",
        body_html="<p>Добрый день!</p><p>Напоминаем об оплате счёта №{invoice_number} от {invoice_date} на сумму {total_amount} {currency}.</p><p>Просим произвести оплату в ближайшее время.</p><p>С уважением,<br/>{sender_name}</p>",
        body_text="Добрый день!\n\nНапоминаем об оплате счёта №{invoice_number} от {invoice_date} на сумму {total_amount} {currency}.\n\nПросим произвести оплату в ближайшее время.\n\nС уважением,\n{sender_name}",
        variables=["invoice_number", "invoice_date", "total_amount", "currency", "sender_name"],
    ),
    "price_inquiry": EmailTemplate(
        name="Запрос цены",
        subject="Запрос коммерческого предложения",
        body_html="<p>Добрый день!</p><p>Просим предоставить коммерческое предложение на следующие позиции:</p><p>{items_list}</p><p>Ожидаем ваш ответ.</p><p>С уважением,<br/>{sender_name}</p>",
        body_text="Добрый день!\n\nПросим предоставить коммерческое предложение на следующие позиции:\n\n{items_list}\n\nОжидаем ваш ответ.\n\nС уважением,\n{sender_name}",
        variables=["items_list", "sender_name"],
    ),
    "order_confirmation": EmailTemplate(
        name="Подтверждение заказа",
        subject="Подтверждение заказа по счёту №{invoice_number}",
        body_html="<p>Добрый день!</p><p>Подтверждаем заказ по счёту №{invoice_number} от {invoice_date}.</p><p>Оплата будет произведена в установленные сроки.</p><p>С уважением,<br/>{sender_name}</p>",
        body_text="Добрый день!\n\nПодтверждаем заказ по счёту №{invoice_number} от {invoice_date}.\n\nОплата будет произведена в установленные сроки.\n\nС уважением,\n{sender_name}",
        variables=["invoice_number", "invoice_date", "sender_name"],
    ),
    "document_request": EmailTemplate(
        name="Запрос документов",
        subject="Запрос документов",
        body_html="<p>Добрый день!</p><p>Просим предоставить следующие документы:</p><p>{documents_list}</p><p>С уважением,<br/>{sender_name}</p>",
        body_text="Добрый день!\n\nПросим предоставить следующие документы:\n\n{documents_list}\n\nС уважением,\n{sender_name}",
        variables=["documents_list", "sender_name"],
    ),
}


@router.post("/suggest-template", response_model=TemplateSuggestResponse)
async def suggest_template(
    payload: TemplateSuggestRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.suggest_template — Suggest email template by context."""
    templates = list(TEMPLATES.values())
    recommended = payload.context_type if payload.context_type in TEMPLATES else None

    # If specific context_type requested, put it first
    if recommended:
        tpl = TEMPLATES[recommended]
        templates = [tpl] + [t for t in templates if t.name != tpl.name]

    return TemplateSuggestResponse(templates=templates, recommended=recommended)


# ── email.delete_message / email.bulk_delete / email.delete_thread ─────────


class EmailBulkDeleteRequest(BaseModel):
    message_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)


@router.delete(
    "/messages/bulk-delete",
    summary="Skill: email.bulk_delete — Bulk delete email messages.",
)
async def bulk_delete_messages(
    payload: EmailBulkDeleteRequest,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk delete email messages and their attached documents."""
    deleted = 0
    hidden = set(await hidden_mailbox_names(db, user))
    for msg_id in payload.message_ids:
        msg = await db.get(EmailMessage, msg_id)
        if not msg or msg.mailbox in hidden:
            continue
        await _delete_message_cascade(msg, db)
        deleted += 1

    await db.commit()
    logger.info("email_messages_bulk_deleted", count=deleted)
    return {"deleted": deleted}


@router.delete(
    "/messages/{message_id}",
    status_code=204,
    summary="Skill: email.delete_message — Delete a single email message.",
)
async def delete_message(
    message_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an email message and its attached documents."""
    msg = await db.get(EmailMessage, message_id)
    if not msg or not await may_read_mailbox(db, user, msg.mailbox):
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    await _delete_message_cascade(msg, db)
    await db.commit()


@router.delete(
    "/threads/{thread_id}",
    status_code=204,
    summary="Skill: email.delete_thread — Delete email thread with all messages.",
)
async def delete_thread(
    thread_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a thread and all its messages including attached documents."""
    thread = await db.get(EmailThread, thread_id)
    if not thread or not await may_read_mailbox(db, user, thread.mailbox):
        raise HTTPException(status_code=404, detail="Тред не найден")

    result = await db.execute(
        select(EmailMessage).where(EmailMessage.thread_id == thread_id)
    )
    messages = result.scalars().all()
    for msg in messages:
        await _delete_message_cascade(msg, db)

    await db.delete(thread)
    await db.commit()
    logger.info("email_thread_deleted", thread_id=str(thread_id), messages=len(messages))


async def _delete_message_cascade(msg: EmailMessage, db) -> None:
    """Delete an email message.

    attachments_meta is stored as JSON on the message; actual document records
    linked to this message (if any) are found via document.metadata_ source reference.
    We intentionally do NOT auto-delete linked documents to avoid data loss —
    the user should delete documents separately via the Documents section.
    """
    await db.delete(msg)
    await db.flush()


@router.post("/messages/{message_id}/attachments/process", response_model=AttachmentProcessResponse)
async def process_email_attachment(
    message_id: uuid.UUID,
    payload: AttachmentProcessRequest,
    current_user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> AttachmentProcessResponse:
    """Skill: email.process_attachment — manually (re)send an already-ingested
    attachment through document extraction or CAD vectorization.

    Every attachment already becomes a Document at IMAP ingest time
    (app.tasks.ingest._store_attachment, content-based classification runs
    automatically). This is for the cases automatic triage doesn't cover: a
    quarantined extension, a failed/low-confidence classification the user
    wants re-run, or turning a drawing attachment into a CAD Drawing record
    (a separate pipeline from Document/invoice extraction).
    """
    from app.db.models import Document, DocumentLink

    msg = (await db.execute(select(EmailMessage).where(EmailMessage.id == message_id))).scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Email message not found")
    if not await may_read_mailbox(db, current_user, msg.mailbox):
        raise HTTPException(status_code=404, detail="Email message not found")

    links = (
        await db.execute(
            select(DocumentLink).where(
                DocumentLink.linked_entity_type == "email_message",
                DocumentLink.linked_entity_id == message_id,
                DocumentLink.link_type == "attachment",
            )
        )
    ).scalars().all()
    doc: Document | None = None
    for link in links:
        candidate = await db.get(Document, link.document_id)
        if candidate and candidate.file_name == payload.filename:
            doc = candidate
            break
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Attachment '{payload.filename}' not found on this message")

    if payload.target == "document":
        from app.tasks.extraction import process_document

        task = process_document.delay(str(doc.id), force=True)
        logger.info("email_attachment_reprocess_document", document_id=str(doc.id), user=current_user.sub)
        return AttachmentProcessResponse(document_id=doc.id, target="document", task_id=task.id)

    if payload.target == "drawing":
        from app.services.drawing_service import create_and_analyze_drawing
        from app.storage import download_file

        fmt = doc.file_name.rsplit(".", 1)[-1].lower() if "." in doc.file_name else ""
        try:
            file_bytes = await asyncio.to_thread(download_file, doc.storage_path)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Storage unavailable: {exc}")
        drawing, task_id = await create_and_analyze_drawing(
            file_bytes=file_bytes,
            filename=doc.file_name,
            fmt=fmt,
            db=db,
            document_id=doc.id,
            created_by=current_user.sub,
        )
        logger.info("email_attachment_to_drawing", document_id=str(doc.id), drawing_id=str(drawing.id))
        return AttachmentProcessResponse(document_id=doc.id, target="drawing", drawing_id=drawing.id, task_id=task_id)

    raise HTTPException(status_code=422, detail="target must be 'document' or 'drawing'")


# ── Attachment bytes + on-demand recognition (agent) ──────────────────────


@router.get("/messages/{message_id}/attachments/{filename}/content")
async def get_attachment_content(
    message_id: uuid.UUID,
    filename: str,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.get_attachment — Stream the raw bytes of one attachment."""
    from fastapi.responses import StreamingResponse

    msg = await db.get(EmailMessage, message_id)
    if not msg or not await may_read_mailbox(db, user, msg.mailbox):
        raise HTTPException(404, "Not found")
    att = (
        await db.execute(
            select(EmailAttachment).where(
                EmailAttachment.message_id == message_id,
                EmailAttachment.filename == filename,
            )
        )
    ).scalars().first()
    if not att or not att.storage_path:
        raise HTTPException(404, "Attachment not found")
    try:
        from app.storage import download_file

        data = await asyncio.to_thread(download_file, att.storage_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Storage unavailable: {exc}")
    return StreamingResponse(
        iter([data]),
        media_type=att.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{att.filename}"'},
    )


@router.post(
    "/messages/{message_id}/attachments/recognize",
    response_model=AttachmentRecognizeResponse,
)
async def recognize_attachment(
    message_id: uuid.UUID,
    payload: AttachmentRecognizeRequest,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.recognize_attachment — OCR/classify/extract an attachment and
    return the text + fields to the agent directly (not just enqueue a task)."""
    msg = await db.get(EmailMessage, message_id)
    if not msg or not await may_read_mailbox(db, user, msg.mailbox):
        raise HTTPException(404, "Not found")

    q = select(EmailAttachment).where(EmailAttachment.message_id == message_id)
    if payload.filename:
        q = q.where(EmailAttachment.filename == payload.filename)
    atts = (await db.execute(q)).scalars().all()
    if not atts:
        raise HTTPException(404, "No attachments")

    results: list[AttachmentRecognitionResult] = []
    for att in atts:
        doc_id = att.document_id
        doc_type: str | None = None
        text: str | None = None
        fields: dict | None = None
        confidence: float | None = None
        if doc_id:
            from app.db.models import Document, DocumentArtifact, Invoice

            doc = await db.get(Document, doc_id)
            if doc is not None:
                doc_type = getattr(doc.doc_type, "value", None) or (
                    str(doc.doc_type) if doc.doc_type else None
                )
                art = (
                    await db.execute(
                        select(DocumentArtifact).where(
                            DocumentArtifact.document_id == doc_id,
                            DocumentArtifact.artifact_type.in_(("extracted_text", "ocr_text")),
                        )
                    )
                ).scalars().first()
                if art is not None:
                    try:
                        from app.storage import download_file

                        text = (await asyncio.to_thread(download_file, art.storage_path)).decode(
                            "utf-8", errors="replace"
                        )[:8000]
                    except Exception:  # noqa: BLE001
                        text = None
                inv = (
                    await db.execute(select(Invoice).where(Invoice.document_id == doc_id))
                ).scalar_one_or_none()
                if inv is not None:
                    fields = {
                        "invoice_number": inv.invoice_number,
                        "total_amount": inv.total_amount,
                        "currency": inv.currency,
                        "due_date": str(inv.due_date.date()) if inv.due_date else None,
                        "supplier_id": str(inv.supplier_id) if inv.supplier_id else None,
                    }
                    confidence = inv.overall_confidence
        results.append(
            AttachmentRecognitionResult(
                filename=att.filename,
                doc_type=doc_type,
                text=text,
                fields=fields,
                confidence=confidence,
                document_id=doc_id,
            )
        )
    return AttachmentRecognizeResponse(results=results)


@router.post("/threads/{thread_id}/reply-draft", response_model=EmailDraftOut)
async def agent_reply_draft(
    thread_id: uuid.UUID,
    payload: AgentComposeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.reply — Draft a reply into an existing thread (correct
    threading headers). Send still goes through the email.send gate."""
    from app.domain.email_compose import ComposeContext, generate_draft_body
    from app.domain.email_send import create_reply_draft

    thread = await db.get(EmailThread, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    last_inbound = (
        await db.execute(
            select(EmailMessage)
            .where(EmailMessage.thread_id == thread_id, EmailMessage.is_inbound == True)  # noqa: E712
            .order_by(EmailMessage.received_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    res = await generate_draft_body(
        db,
        intent=payload.intent,
        context=ComposeContext(thread_id=thread_id, supplier_id=payload.supplier_id,
                               invoice_id=payload.invoice_id, mailbox=thread.mailbox),
        tone_override=payload.tone,
        acting_user_sub=(request.headers.get("x-acting-user") or "").strip() or None,
    )
    to = payload.to_addresses or ([last_inbound.from_address] if last_inbound else [])
    subject = res.subject or (
        f"Re: {thread.subject}" if not thread.subject.lower().startswith("re:") else thread.subject
    )
    draft = await create_reply_draft(
        db,
        to_addresses=to,
        subject=subject,
        body_html=res.body_html,
        body_text=res.body_text,
        thread_id=thread_id,
        mailbox=thread.mailbox,
        in_reply_to_message_id=last_inbound.id if last_inbound else None,
        attachment_ids=payload.attachment_ids,
    )
    await db.commit()
    await db.refresh(draft)
    return _draft_to_out(draft)


# ── email.read (must be last — catch-all path) ────────────────────────────


@router.get("/{email_id}", response_model=EmailMessageOut)
async def read_email(
    email_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.read — Read email message with attachments."""
    result = await db.execute(select(EmailMessage).where(EmailMessage.id == email_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Email message not found")
    if not await may_read_mailbox(db, user, msg.mailbox):
        raise HTTPException(status_code=404, detail="Email message not found")
    return msg
