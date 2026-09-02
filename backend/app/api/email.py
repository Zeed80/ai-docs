"""Email API — skills: email.fetch_new, email.read, email.search,
email.draft, email.style_match, email.risk_check, email.send, email.suggest_template"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, delete as sa_delete, select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.acting import get_effective_user
from app.auth.models import UserInfo
from app.db.session import get_db
from app.domain.email_counts import invalidate_mailbox_counts, mailbox_counts
from app.domain.email_access import (
    hidden_mailbox_names,
    mailbox_filter,
    draft_access_filter,
    may_access_draft,
    usable_attachment_ids,
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
    DerivedInvoiceOut,
    ThreadListResponse,
    TriageResultOut,
)
from app.audit.service import log_action, add_timeline_event

router = APIRouter()
logger = structlog.get_logger()


@router.post("/fetch", response_model=EmailFetchResponse)
async def fetch_new_emails(
    payload: EmailFetchRequest,
    request: Request,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.fetch_new — poll IMAP now and report what arrived.

    Ф6.9 — this used to dispatch the task and unconditionally return
    ``fetched_count=0``. The agent, told it had "проверить новую почту",
    read that as "новых писем нет" and reported it to the user. Now it waits
    briefly for the poll it started and answers honestly; if the poll is still
    running it says so instead of guessing.

    Отчитываемся ТОЛЬКО за ящики, видимые вызывающему. Опрос по-прежнему
    наполняет базу по всем активным ящикам (иначе почтовый клиент перестанет
    показывать письма), но число в ответе складывается по видимым: иначе агент
    сообщал «пришло 5 писем», а list/search показывал ноль — расхождение,
    которое человек может объяснить только «агент врёт».
    """
    from app.tasks.email_triage import run_triage

    if payload.mailbox and not await may_read_mailbox(
        db, user, payload.mailbox, for_agent=request_is_agent(request)
    ):
        raise HTTPException(404, "Mailbox not found")

    task = run_triage.delay(payload.mailbox)
    logger.info("email_triage_triggered", mailbox=payload.mailbox, task_id=task.id)

    result: dict | None = None
    try:
        from celery.result import AsyncResult

        from app.tasks.celery_app import celery_app

        async_result = AsyncResult(task.id, app=celery_app)
        for _ in range(24):          # ~12 s: an IMAP poll is normally quicker
            if async_result.ready():
                raw = async_result.result
                result = raw if isinstance(raw, dict) else None
                break
            await asyncio.sleep(0.5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("email_fetch_wait_failed", task_id=task.id, error=str(exc))

    if result is None:
        return EmailFetchResponse(
            fetched_count=0,
            new_messages=[],
            errors=["Проверка почты ещё идёт — результат будет через несколько секунд"],
            task_id=task.id,
        )

    by_mailbox = result.get("by_mailbox")
    if isinstance(by_mailbox, dict):
        hidden = set(
            await hidden_mailbox_names(db, user, for_agent=request_is_agent(request))
        )
        fetched = sum(int(v or 0) for k, v in by_mailbox.items() if k not in hidden)
    else:
        fetched = int(result.get("total_emails") or 0)

    return EmailFetchResponse(
        fetched_count=fetched,
        new_messages=[],
        errors=[str(e) for e in (result.get("errors") or [])][:10],
        task_id=task.id,
    )


def _documents_matching(like: str):
    """Message ids whose emailed attachments were recognised as matching text.

    Postgres only: the recognised content is JSON, and casting it per row is a
    scan unless the trigram index on the cast is in place (migration
    20260829_0003).
    """
    from app.db.models import Document, DocumentExtraction

    return (
        select(Document.source_email_id)
        .join(DocumentExtraction, DocumentExtraction.document_id == Document.id)
        .where(
            Document.source_email_id.isnot(None),
            cast(DocumentExtraction.structured_data, String).ilike(like),
        )
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
            like = f"%{payload.query}%"
            # FTS for natural-language RU, trigram-accelerated ILIKE for article
            # numbers / latin / substrings the tsvector lexer would miss.
            query = query.where(
                or_(
                    tsv.op("@@")(tsq),
                    EmailMessage.subject.ilike(like),
                    EmailMessage.body_text.ilike(like),
                    EmailMessage.from_address.ilike(like),
                    # Ф8 — "письмо, к которому был приложен счёт-2562.pdf" was
                    # unanswerable: attachment filenames were not searchable at
                    # all. Inline parts are excluded so a signature logo does
                    # not match every letter from that sender.
                    EmailMessage.id.in_(
                        select(EmailAttachment.message_id).where(
                            EmailAttachment.filename.ilike(like),
                            EmailAttachment.is_inline == False,  # noqa: E712
                            EmailAttachment.message_id.isnot(None),
                        )
                    ),
                    # Ф8 — the CONTENTS of an attachment, not just its name:
                    # "письмо, где был счёт на 2667 рублей". The recognised
                    # content of a document lives in document_extractions
                    # (structured_data), not in a text column on documents —
                    # see "Находки по ходу".
                    EmailMessage.id.in_(_documents_matching(like)),
                )
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
        # Ф8 — JSON→text cast cannot use an index and matches inside any part of
        # the serialised array. Postgres can search the array itself.
        if is_pg:
            from sqlalchemy.dialects.postgresql import JSONB

            query = query.where(
                func.lower(
                    func.jsonb_path_query_array(
                        cast(EmailMessage.to_addresses, JSONB), "$[*]"
                    ).cast(String)
                ).like(f"%{payload.to_addr.lower()}%")
            )
        else:
            query = query.where(
                cast(EmailMessage.to_addresses, String).ilike(f"%{payload.to_addr}%")
            )
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

    # Ф5.1 — ``cursor``/``next_cursor`` were declared in the schema and never
    # implemented, so search could only ever return its first page. Offset here
    # rather than keyset: relevance sorting has no stable key to seek on, and a
    # search result set is a snapshot the user is scrolling, not a live feed.
    page_size = max(1, min(payload.limit, 200))
    offset = 0
    if payload.cursor:
        try:
            offset = max(0, int(payload.cursor))
        except (TypeError, ValueError):
            offset = 0

    result = await db.execute(query.offset(offset).limit(page_size))
    messages = result.scalars().all()
    next_cursor = (
        str(offset + page_size) if offset + len(messages) < total else None
    )
    return EmailSearchResponse(results=messages, total=total, next_cursor=next_cursor)


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

    counts = await mailbox_counts(db, names)

    return [
        EmailMailboxChip(
            name=c.name,
            display_name=c.display_name,
            is_personal=c.mailbox_type == "personal",
            thread_count=counts.for_mailbox(c.name)["threads"],
            message_count=counts.for_mailbox(c.name)["messages"],
            unread_count=counts.for_mailbox(c.name)["unread"],
            last_sync_at=c.last_sync_at,
            sync_error=c.sync_error,
        )
        for c in cfgs
    ]


class FolderCounts(BaseModel):
    folder: str
    total: int
    unread: int


@router.get("/folder-counts", response_model=list[FolderCounts])
async def folder_counts(
    request: Request,
    mailbox: str | None = None,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-folder thread counts for the sidebar."""
    base = select(
        EmailThread.folder,
        func.count(EmailThread.id),
        func.count(EmailThread.id).filter(EmailThread.is_read == False),  # noqa: E712
    )
    scope = await mailbox_filter(
        db, user, mailbox_col=EmailThread.mailbox, for_agent=request_is_agent(request)
    )
    if scope is not None:
        base = base.where(scope)
    if mailbox:
        base = base.where(EmailThread.mailbox == mailbox)
    rows = (await db.execute(base.group_by(EmailThread.folder))).all()
    out = {f: FolderCounts(folder=f, total=0, unread=0) for f in _SYSTEM_FOLDERS}
    for folder, total, unread in rows:
        out[folder or "inbox"] = FolderCounts(
            folder=folder or "inbox", total=int(total or 0), unread=int(unread or 0)
        )
    return list(out.values())


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


async def _thread_label_counts(
    db: AsyncSession, label_ids: list[uuid.UUID], *, scope=None
) -> dict:
    """Ф8 — counts must match what the person can actually open.

    Counting every thread carrying a label, including ones in a colleague's
    personal mailbox, made the number next to a label disagree with the list it
    opens — and leaked a rough size of someone else's correspondence.
    """
    if not label_ids:
        return {}
    query = (
        select(EmailThreadLabel.label_id, func.count())
        .join(EmailThread, EmailThread.id == EmailThreadLabel.thread_id)
        .where(EmailThreadLabel.label_id.in_(label_ids))
    )
    if scope is not None:
        query = query.where(scope)
    rows = (await db.execute(query.group_by(EmailThreadLabel.label_id))).all()
    return {lid: int(n) for lid, n in rows}


def _encode_cursor(last_message_at, thread_id) -> str:
    """Opaque keyset cursor: (last_message_at, id) is the list's sort order."""
    import base64

    raw = f"{last_message_at.isoformat() if last_message_at else ''}|{thread_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime | None, uuid.UUID] | None:
    import base64

    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp, _, ident = raw.partition("|")
        return (datetime.fromisoformat(stamp) if stamp else None), uuid.UUID(ident)
    except Exception:  # noqa: BLE001
        return None


@router.get("/threads", response_model=ThreadListResponse)
async def list_threads(
    request: Request,
    mailbox: str | None = None,
    folder: str | None = None,
    label_id: uuid.UUID | None = None,
    is_unread: bool | None = None,
    is_starred: bool | None = None,
    has_attachments: bool | None = None,
    limit: int = 50,
    cursor: str | None = None,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.list_threads — List email threads (with client filters).

    Ф5.1 — keyset pagination. The endpoint returned a bare capped list, and the
    client had no way to ask for more, so a conversation older than the first
    page was unreachable: on a real mailbox "письмо было в марте" simply could
    not be opened. Keyset (not OFFSET) because new mail keeps arriving at the
    top while a person is paging.
    """
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
    if folder == "sent":
        # "Отправленные" = any non-trashed thread with an outbound message (a
        # reply you sent in an inbox thread counts too), plus threads you started.
        query = query.where(
            EmailThread.folder.notin_(("trash", "spam")),
            or_(
                EmailThread.folder == "sent",
                EmailThread.id.in_(
                    select(EmailMessage.thread_id).where(EmailMessage.is_inbound == False)  # noqa: E712
                ),
            ),
        )
    else:
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

    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded is not None:
            last_at, last_id = decoded
            if last_at is not None:
                query = query.where(
                    or_(
                        EmailThread.last_message_at < last_at,
                        and_(
                            EmailThread.last_message_at == last_at,
                            EmailThread.id < last_id,
                        ),
                    )
                )

    page_size = max(1, min(limit, 200))
    query = query.order_by(
        EmailThread.last_message_at.desc().nullslast(), EmailThread.id.desc()
    ).limit(page_size + 1)
    rows = list((await db.execute(query)).scalars().all())

    next_cursor = None
    if len(rows) > page_size:
        rows = rows[:page_size]
        last = rows[-1]
        next_cursor = _encode_cursor(last.last_message_at, last.id)

    return ThreadListResponse(
        items=[_thread_out(t) for t in rows],
        total=len(rows),
        next_cursor=next_cursor,
    )


async def _attach_derived_invoices(db: AsyncSession, thread_out: EmailThreadOut) -> None:
    """Fill in "из этого письма завели счёт" for every message in the thread.

    Ф6.3. The pipeline linked Document → EmailMessage but nothing linked back,
    so a person reading the thread could not tell that an invoice had been
    created from it — the very thing the agent is supposed to have done for
    them — and had no way to open it.
    """
    from app.db.models import Document, Invoice, Party

    message_ids = [m.id for m in thread_out.messages]
    if not message_ids:
        return
    rows = (
        await db.execute(
            select(Invoice, Document.source_email_id, Party.name)
            .join(Document, Invoice.document_id == Document.id)
            .outerjoin(Party, Invoice.supplier_id == Party.id)
            .where(Document.source_email_id.in_(message_ids))
        )
    ).all()
    by_message: dict = {}
    for invoice, source_email_id, supplier_name in rows:
        by_message.setdefault(source_email_id, []).append(
            DerivedInvoiceOut(
                invoice_id=invoice.id,
                document_id=invoice.document_id,
                invoice_number=invoice.invoice_number,
                total_amount=float(invoice.total_amount) if invoice.total_amount is not None else None,
                currency=invoice.currency,
                status=invoice.status.value if hasattr(invoice.status, "value") else str(invoice.status),
                supplier_name=supplier_name,
                supplier_matched_by=(invoice.metadata_ or {}).get("supplier_matched_by"),
            )
        )
    for msg in thread_out.messages:
        msg.derived_invoices = by_message.get(msg.id, [])

    # Ф6.4 — what the agent understood about each letter and what it did.
    from app.db.models import EmailTriageResult
    from app.domain.email_triage import label_for

    triage_rows = (
        await db.execute(
            select(EmailTriageResult).where(
                EmailTriageResult.message_id.in_(message_ids)
            )
        )
    ).scalars().all()
    by_msg_triage = {t.message_id: t for t in triage_rows}
    for msg in thread_out.messages:
        row = by_msg_triage.get(msg.id)
        if row is None:
            continue
        msg.triage = TriageResultOut(
            category=row.corrected_category or row.category,
            category_label=label_for(row.corrected_category or row.category),
            confidence=row.confidence,
            summary=row.summary,
            entities=row.entities or {},
            performed=row.performed or [],
            proposed=row.proposed or [],
            model_name=row.model_name,
            corrected_category=row.corrected_category,
            status=row.status,
        )


class EmailUserPrefs(BaseModel):
    """Ф1.4 — пользовательские настройки чтения почты. Живут в
    ``users.preferences`` (JSON), где уже лежат прочие настройки, — отдельная
    таблица ради одного флага была бы дороже, чем польза от неё."""

    always_show_images: bool = False


@router.get("/preferences", response_model=EmailUserPrefs)
async def get_email_preferences(
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> EmailUserPrefs:
    """Skill: email.get_preferences — Per-user mail reading preferences."""
    from app.db.models import User

    prefs = (
        await db.execute(select(User.preferences).where(User.sub == user.sub))
    ).scalar_one_or_none()
    return EmailUserPrefs(
        always_show_images=bool((prefs or {}).get("email_always_show_images")),
    )


@router.patch("/preferences", response_model=EmailUserPrefs)
async def update_email_preferences(
    payload: EmailUserPrefs,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> EmailUserPrefs:
    """Skill: email.set_preferences — Update per-user mail reading preferences."""
    from app.db.models import User

    row = (
        await db.execute(select(User).where(User.sub == user.sub))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Пользователь не найден")
    # Reassign rather than mutate: SQLAlchemy does not track in-place changes
    # to a plain JSON column, so a mutation here would silently not persist.
    row.preferences = {
        **(row.preferences or {}),
        "email_always_show_images": payload.always_show_images,
    }
    await db.commit()
    return payload


async def _mark_trusted_senders(db: AsyncSession, user: UserInfo, out) -> None:
    """Ф1.4 — кому из отправителей этого треда картинки показывать сразу.

    Блокировка по умолчанию защищает от трекинг-пикселя. Без исключений её
    выключают целиком — и защита пропадает вся сразу, включая незнакомцев.
    """
    from email.utils import parseaddr

    from app.db.models import EmailContact, User

    def _bare_address(addr: str) -> str:
        return parseaddr(addr or "")[1] or addr or ""

    if not out.messages:
        return

    pref_row = (
        await db.execute(select(User.preferences).where(User.sub == user.sub))
    ).scalar_one_or_none()
    if isinstance(pref_row, dict) and pref_row.get("email_always_show_images"):
        for m in out.messages:
            m.images_trusted = True
        return

    senders = {
        _bare_address(m.from_address).lower() for m in out.messages if m.from_address
    }
    if not senders:
        return
    trusted = set(
        (
            await db.execute(
                select(func.lower(EmailContact.email)).where(
                    func.lower(EmailContact.email).in_(senders),
                    EmailContact.trust_images == True,  # noqa: E712
                    or_(
                        EmailContact.owner_sub == user.sub,
                        EmailContact.owner_sub.is_(None),
                    ),
                )
            )
        ).scalars().all()
    )
    for m in out.messages:
        if m.from_address and _bare_address(m.from_address).lower() in trusted:
            m.images_trusted = True


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
    out = _thread_out(thread, with_messages=True)
    await _attach_derived_invoices(db, out)
    await _mark_trusted_senders(db, user, out)
    return out


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
    # Ф2.2 — what we change here must also reach the server. Collected during
    # the loop and queued once, so a bulk action is one push, not N.
    sync_ops: list[tuple] = []
    for t in threads:
        if payload.action in ("read", "unread"):
            val = payload.action == "read"
            t.is_read = val
            t.unread_count = 0 if val else max(t.message_count, 1)
            for m in t.messages or []:
                m.is_read = val
                sync_ops.append((m, "seen" if val else "unseen", None))
        elif payload.action in ("star", "unstar"):
            t.is_starred = payload.action == "star"
            for m in t.messages or []:
                m.is_starred = t.is_starred
                sync_ops.append(
                    (m, "flagged" if t.is_starred else "unflagged", None)
                )
        elif payload.action in ("archive", "trash", "spam", "inbox"):
            t.folder = "archive" if payload.action == "archive" else (
                "inbox" if payload.action == "inbox" else payload.action
            )
            for m in t.messages or []:
                m.folder = t.folder
                sync_ops.append((m, "move", t.folder))
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

    if sync_ops:
        await _queue_sync_ops(db, sync_ops)

    await db.commit()
    if updated:
        await invalidate_mailbox_counts()

    if sync_ops:
        try:
            from app.tasks.email_sync import push_ops

            push_ops.apply_async(kwargs={"mailbox": threads[0].mailbox}, queue="mail")
        except Exception as exc:  # noqa: BLE001
            # The op rows are already committed; the periodic push picks them up.
            logger.warning("email_push_dispatch_failed", error=str(exc))

    try:
        from app.core.chat_bus import chat_bus

        for t in threads:
            await chat_bus.publish({"type": "email.thread_updated", "thread_id": str(t.id),
                                    "mailbox": t.mailbox})
    except Exception:  # noqa: BLE001
        pass
    logger.info("email_bulk_thread_action", action=payload.action, updated=updated, actor=actor)
    return BulkActionResult(updated=updated)


class TriageCorrection(BaseModel):
    category: str


@router.post("/messages/{message_id}/triage/correct", response_model=TriageResultOut)
async def correct_triage(
    message_id: uuid.UUID,
    payload: TriageCorrection,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """A human disagrees with how the agent classified a letter.

    This is the training signal (Ф6.8): a correction is the only reliable
    evidence that the classifier was wrong, and it is worth nothing unless it
    is captured where the classifier can later be measured against it.
    """
    from app.db.models import EmailTriageResult
    from app.domain.email_triage import CATEGORIES, label_for

    if payload.category not in CATEGORIES:
        raise HTTPException(422, f"Неизвестная категория: {payload.category}")

    msg = await db.get(EmailMessage, message_id)
    if not msg or not await may_read_mailbox(db, user, msg.mailbox):
        raise HTTPException(404, "Not found")

    row = (
        await db.execute(
            select(EmailTriageResult).where(EmailTriageResult.message_id == message_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Письмо не разбиралось агентом")

    row.corrected_category = payload.category
    row.corrected_by = user.sub
    await log_action(
        db, action="email.triage_corrected", entity_type="email", entity_id=message_id,
        details={"from": row.category, "to": payload.category, "by": user.sub},
    )
    await db.commit()
    await db.refresh(row)

    try:
        from app.domain.email_learning import record_triage_correction

        await record_triage_correction(db, row, msg, corrected_by=user.sub)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("triage_correction_learning_failed", error=str(exc))

    return TriageResultOut(
        category=row.corrected_category,
        category_label=label_for(row.corrected_category),
        confidence=row.confidence,
        summary=row.summary,
        entities=row.entities or {},
        performed=row.performed or [],
        proposed=row.proposed or [],
        model_name=row.model_name,
        corrected_category=row.corrected_category,
        status=row.status,
    )


async def _queue_sync_ops(db: AsyncSession, ops: list[tuple]) -> None:
    """Queue write-back operations for messages that exist on a server.

    A message with no ``imap_uid`` (our own outbound copy, or one ingested
    before Ф2.1) cannot be addressed remotely; queueing an op for it would
    guarantee a permanent failure, so it is skipped silently — the local state
    is still correct.
    """
    from app.db.models import EmailSyncOp, MailboxFolder

    targets = [(m, op, extra) for m, op, extra in ops if m.imap_uid and m.imap_folder]
    if not targets:
        return

    # Resolve our folder name → the server's, once per mailbox.
    wanted_local = {extra for _, op, extra in targets if op == "move" and extra}
    remote_by_local: dict[tuple[str, str], str] = {}
    if wanted_local:
        rows = (
            await db.execute(
                select(MailboxFolder.mailbox, MailboxFolder.local_folder,
                       MailboxFolder.remote_name)
                .where(MailboxFolder.local_folder.in_(wanted_local))
            )
        ).all()
        for box, local, remote in rows:
            remote_by_local.setdefault((box, local), remote)

    for msg, op, extra in targets:
        payload = None
        if op == "move":
            remote = remote_by_local.get((msg.mailbox, extra))
            if not remote:
                # No server folder mapped for this — better to leave the letter
                # where it is than to invent a destination.
                logger.info(
                    "email_move_not_mapped", mailbox=msg.mailbox, local_folder=extra,
                )
                continue
            payload = {"remote_folder": remote, "local_folder": extra}
        db.add(EmailSyncOp(
            message_id=msg.id, mailbox=msg.mailbox, op=op, payload=payload,
        ))


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
    scope = await mailbox_filter(db, user, mailbox_col=EmailThread.mailbox)
    counts = await _thread_label_counts(db, [l.id for l in rows], scope=scope)
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


# ── Email policy (admin: auto-send, attachment retention) ──────────────────


class EmailPolicyOut(BaseModel):
    auto_send_enabled: bool = False
    auto_send_max_per_day: int = 20
    attachment_retention_days: int = 180


class EmailPolicyUpdate(BaseModel):
    auto_send_enabled: bool | None = None
    auto_send_max_per_day: int | None = None
    attachment_retention_days: int | None = None


@router.get("/policy", response_model=EmailPolicyOut)
async def get_email_policy(
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    from app.db.models import MailServerConfig

    row = (await db.execute(select(MailServerConfig))).scalars().first()
    if not row:
        return EmailPolicyOut()
    return EmailPolicyOut(
        auto_send_enabled=row.auto_send_enabled,
        auto_send_max_per_day=row.auto_send_max_per_day,
        attachment_retention_days=row.attachment_retention_days,
    )


@router.put("/policy", response_model=EmailPolicyOut)
async def update_email_policy(
    payload: EmailPolicyUpdate,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Protected: only admins. Turning on auto_send lets email filter rules send
    templated replies without a human — with the guardrails in email_rules."""
    from app.auth.models import UserRole
    from app.db.models import MailServerConfig

    if UserRole.admin not in (user.roles or []):
        raise HTTPException(403, "Только администратор")
    row = (await db.execute(select(MailServerConfig))).scalars().first()
    if not row:
        row = MailServerConfig(singleton_key="default")
        db.add(row)
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(row, k, v)
    await db.commit()
    await log_action(
        db, action="email.policy_update", entity_type="email_policy", entity_id=row.id,
        details={**data, "by": user.sub},
    )
    logger.info("email_policy_updated", by=user.sub, **data)
    return EmailPolicyOut(
        auto_send_enabled=row.auto_send_enabled,
        auto_send_max_per_day=row.auto_send_max_per_day,
        attachment_retention_days=row.attachment_retention_days,
    )


# ── Signatures ─────────────────────────────────────────────────────────────


class SignatureOut(BaseModel):
    id: uuid.UUID
    name: str
    body_html: str
    mailbox: str | None
    owner_sub: str | None
    is_default: bool

    model_config = {"from_attributes": True}


class SignatureCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    body_html: str
    mailbox: str | None = None
    is_default: bool = False
    shared: bool = False


class SignatureUpdate(BaseModel):
    name: str | None = None
    body_html: str | None = None
    mailbox: str | None = None
    is_default: bool | None = None


@router.get("/signatures", response_model=list[SignatureOut])
async def list_signatures(
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.signatures_list — Signatures the caller can use."""
    from app.db.models import EmailSignature

    rows = (
        await db.execute(
            select(EmailSignature).where(
                or_(EmailSignature.owner_sub.is_(None), EmailSignature.owner_sub == user.sub)
            ).order_by(EmailSignature.is_default.desc(), EmailSignature.name)
        )
    ).scalars().all()
    return list(rows)


@router.get("/signatures/resolve", response_model=SignatureOut | None)
async def resolve_signature(
    mailbox: str | None = None,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """The signature to prefill for a compose from ``mailbox``: mailbox-specific
    default > user's personal default > None."""
    sig = await _resolve_signature(db, user.sub, mailbox)
    return sig


async def _resolve_signature(db: AsyncSession, user_sub: str | None, mailbox: str | None):
    from app.db.models import EmailSignature

    if mailbox:
        row = (
            await db.execute(
                select(EmailSignature)
                .where(EmailSignature.mailbox == mailbox, EmailSignature.owner_sub.is_(None))
                .order_by(EmailSignature.is_default.desc())
            )
        ).scalars().first()
        if row:
            return row
    if user_sub:
        row = (
            await db.execute(
                select(EmailSignature)
                .where(EmailSignature.owner_sub == user_sub)
                .order_by(EmailSignature.is_default.desc())
            )
        ).scalars().first()
        if row:
            return row
    return None


@router.post("/signatures", response_model=SignatureOut, status_code=201)
async def create_signature(
    payload: SignatureCreate,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    from app.auth.models import UserRole
    from app.db.models import EmailSignature

    is_shared = (payload.shared or payload.mailbox) and UserRole.admin in (user.roles or [])
    owner = None if is_shared else user.sub
    sig = EmailSignature(
        name=payload.name, body_html=payload.body_html,
        mailbox=payload.mailbox if is_shared else None,
        owner_sub=owner, is_default=payload.is_default,
    )
    if payload.is_default:
        await _clear_default_signatures(db, owner, sig.mailbox)
    db.add(sig)
    await db.commit()
    await db.refresh(sig)
    return sig


@router.patch("/signatures/{sig_id}", response_model=SignatureOut)
async def update_signature(
    sig_id: uuid.UUID,
    payload: SignatureUpdate,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    from app.auth.models import UserRole
    from app.db.models import EmailSignature

    sig = await db.get(EmailSignature, sig_id)
    if not sig:
        raise HTTPException(404, "Signature not found")
    if sig.owner_sub not in (None, user.sub) or (sig.owner_sub is None and UserRole.admin not in (user.roles or [])):
        raise HTTPException(403, "Нет прав на эту подпись")
    data = payload.model_dump(exclude_none=True)
    if data.get("is_default"):
        await _clear_default_signatures(db, sig.owner_sub, data.get("mailbox", sig.mailbox))
    for k, v in data.items():
        setattr(sig, k, v)
    await db.commit()
    await db.refresh(sig)
    return sig


@router.delete("/signatures/{sig_id}", status_code=204)
async def delete_signature(
    sig_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    from app.auth.models import UserRole
    from app.db.models import EmailSignature

    sig = await db.get(EmailSignature, sig_id)
    if not sig:
        raise HTTPException(404, "Signature not found")
    if sig.owner_sub not in (None, user.sub) or (sig.owner_sub is None and UserRole.admin not in (user.roles or [])):
        raise HTTPException(403, "Нет прав на эту подпись")
    await db.delete(sig)
    await db.commit()


async def _clear_default_signatures(db: AsyncSession, owner_sub, mailbox):
    from app.db.models import EmailSignature

    q = select(EmailSignature).where(EmailSignature.is_default == True)  # noqa: E712
    q = q.where(EmailSignature.owner_sub == owner_sub) if owner_sub else q.where(EmailSignature.owner_sub.is_(None))
    if mailbox:
        q = q.where(EmailSignature.mailbox == mailbox)
    for row in (await db.execute(q)).scalars().all():
        row.is_default = False


# ── Compose attachments ────────────────────────────────────────────────────


@router.post("/attachments/upload", response_model=EmailAttachmentOut)
async def upload_compose_attachment(
    file: UploadFile = File(...),
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.attachment_upload — Stage a file for an outbound email.

    Read in chunks with a running total: ``await file.read()`` buffered the
    whole upload into the API worker's memory and only then compared it to the
    limit, so the limit protected nothing — a multi-gigabyte POST was a
    denial-of-service against the process, not a 413.
    """
    import hashlib

    from app.db.models import MailServerConfig

    cfg = (await db.execute(select(MailServerConfig))).scalars().first()
    limit_mb = (cfg.max_attachment_mb if cfg else 25) or 25
    limit = limit_mb * 1024 * 1024

    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, f"Вложение больше {limit_mb} МБ")
        hasher.update(chunk)
        chunks.append(chunk)
    content = b"".join(chunks)
    sha = hasher.hexdigest()
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
        uploaded_by_sub=user.sub,
    )
    db.add(att)
    await db.commit()
    await db.refresh(att)
    return EmailAttachmentOut.model_validate(att)


# ── email.draft ────────────────────────────────────────────────────────────


async def _assert_attachments_usable(db: AsyncSession, user: UserInfo, attachment_ids) -> None:
    """403 unless every referenced attachment is one this caller may send."""
    requested = list(attachment_ids or [])
    if not requested:
        return
    allowed = set(await usable_attachment_ids(db, user, requested))
    missing = [a for a in requested if a not in allowed]
    if missing:
        raise HTTPException(403, "Вложение недоступно")


@router.post("/drafts", response_model=EmailDraftOut)
async def create_draft(
    payload: EmailDraftCreate,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.draft — Create email draft."""
    from app.domain.email_send import create_reply_draft

    if payload.mailbox and not await may_write_mailbox(db, user, payload.mailbox):
        raise HTTPException(403, "Нет доступа для отправки из этого ящика")
    await _assert_attachments_usable(db, user, payload.attachment_ids)

    # Черновик без ящика не имеет SMTP-аккаунта: отправка сваливалась в
    # глобальный .env, а когда его нет — в мнимую отправку, о которой человеку
    # сообщали как об успешной. Агент ящик обычно не указывает, поэтому
    # create_reply_draft резолвит его сам — до расчёта content_digest, чтобы
    # подтверждение человека относилось в том числе к адресу отправителя.
    mailbox = payload.mailbox

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
        mailbox=mailbox,
        in_reply_to_message_id=payload.in_reply_to_message_id,
        forward_of_message_id=payload.forward_of_message_id,
        attachment_ids=payload.attachment_ids,
        owner_sub=user.sub,
    )
    await db.commit()
    await db.refresh(draft)
    logger.info("email_draft_created", draft_id=str(draft.id), by=user.sub)
    return _draft_to_out(draft)


@router.patch("/drafts/{draft_id}", response_model=EmailDraftOut)
async def update_draft(
    draft_id: uuid.UUID,
    payload: EmailDraftUpdate,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.draft_update — Autosave / edit a draft in place."""
    from sqlalchemy.orm.attributes import flag_modified

    draft = await db.get(DraftAction, draft_id)
    if not draft or draft.action_type != "email.send":
        raise HTTPException(404, "Draft not found")
    # 404, not 403: a draft the caller may not touch must not be confirmed to
    # exist by the error code.
    if not await may_access_draft(db, user, draft.draft_data):
        raise HTTPException(404, "Draft not found")
    if payload.mailbox and not await may_write_mailbox(db, user, payload.mailbox):
        raise HTTPException(403, "Нет доступа для отправки из этого ящика")
    if payload.attachment_ids is not None:
        await _assert_attachments_usable(db, user, payload.attachment_ids)
    if draft.executed:
        raise HTTPException(400, "Письмо уже отправлено")
    data = dict(draft.draft_data or {})
    patch = payload.model_dump(exclude_none=True)
    if "attachment_ids" in patch:
        patch["attachment_ids"] = [str(a) for a in patch["attachment_ids"]]
    data.update(patch)
    data["status"] = "draft"  # any edit re-opens risk check
    from app.domain.email_send import draft_content_digest

    data["content_digest"] = draft_content_digest(data)
    draft.draft_data = data
    flag_modified(draft, "draft_data")
    await db.commit()
    await db.refresh(draft)
    return _draft_to_out(draft)


@router.get("/drafts", response_model=list[EmailDraftOut])
async def list_drafts(
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.list_drafts — Drafts the caller may see (own drafts plus
    drafts in mailboxes they may send from). Before Ф0.1 this returned every
    draft in the system, including drafts inside colleagues' personal
    mailboxes."""
    result = await db.execute(
        select(DraftAction)
        .where(DraftAction.action_type == "email.send", DraftAction.executed == False)
        .order_by(DraftAction.created_at.desc())
    )
    drafts = result.scalars().all()
    may = await draft_access_filter(db, user)
    return [_draft_to_out(d) for d in drafts if may(d.draft_data)]


@router.get("/drafts/{draft_id}", response_model=EmailDraftOut)
async def get_draft(
    draft_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Get email draft."""
    result = await db.execute(
        select(DraftAction).where(DraftAction.id == draft_id)
    )
    draft = result.scalar_one_or_none()
    if not draft or not await may_access_draft(db, user, draft.draft_data):
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
        content_digest=data.get("content_digest"),
        created_at=draft.created_at,
    )


# ── Direct human send (no agent gate) ──────────────────────────────────────


# Undo window in seconds. 30 is a hard ceiling for the interactive case: longer
# and people close the tab believing the message is gone.
_MAX_UNDO_SECONDS = 30
# Scheduled sends are a different thing and may sit for days.
_MAX_SCHEDULE_SECONDS = 60 * 60 * 24 * 30


def _resolve_send_delay(payload: ComposeSendRequest) -> int:
    """Seconds to hold the message before handing it to SMTP.

    Two different intents share one mechanism: a short "Отменить" window and an
    explicit "отправить позже". Clamped so a typo cannot park a letter for a
    year, and never negative for a time already past.
    """
    if payload.send_at is not None:
        target = payload.send_at
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        seconds = int((target - datetime.now(timezone.utc)).total_seconds())
        return max(0, min(seconds, _MAX_SCHEDULE_SECONDS))
    if payload.delay_seconds is None:
        return 0
    return max(0, min(int(payload.delay_seconds), _MAX_UNDO_SECONDS))


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
    await _assert_attachments_usable(db, user, payload.attachment_ids)

    thread_id = None
    if payload.in_reply_to_message_id:
        parent = await db.get(EmailMessage, payload.in_reply_to_message_id)
        if parent:
            thread_id = parent.thread_id

    if payload.draft_id:
        draft = await db.get(DraftAction, payload.draft_id)
        if not draft or draft.action_type != "email.send":
            raise HTTPException(404, "Draft not found")
        if not await may_access_draft(db, user, draft.draft_data):
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
        from app.domain.email_send import draft_content_digest

        data["content_digest"] = draft_content_digest(data)
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
            owner_sub=user.sub,
        )
        d = dict(draft.draft_data or {})
        d["sent_by"] = "user"
        draft.draft_data = d
        flag_modified(draft, "draft_data")

    await db.commit()
    await db.refresh(draft)

    # Ф4 — the same detectors the agent goes through. A person hitting Send used
    # to bypass all of them: the path with the higher error rate had no checks,
    # while the gated one had every check.
    from app.domain.email_risk import blocking_flags, evaluate_draft

    detected = await evaluate_draft(db, draft.draft_data or {})
    acknowledged = {c.strip() for c in (payload.acknowledged_risks or [])}
    blocking = [f for f in blocking_flags(detected) if f.code not in acknowledged]
    if blocking:
        d = dict(draft.draft_data or {})
        d["status"] = "risk_checked"
        d["risk_flags"] = [
            {"code": f.code, "severity": f.severity, "message": f.message,
             "can_override": f.can_override}
            for f in detected
        ]
        draft.draft_data = d
        flag_modified(draft, "draft_data")
        await db.commit()
        return EmailSendResult(
            draft_id=draft.id, status="blocked",
            blocked_by=[{"code": f.code, "message": f.message} for f in blocking],
            warnings=[
                {"code": f.code, "message": f.message}
                for f in detected if not f.blocking
            ],
        )

    if acknowledged:
        await log_action(
            db, action="email.risk_override", entity_type="email", entity_id=draft.id,
            details={"codes": sorted(acknowledged), "by": user.sub},
        )

    from app.tasks.email_sender import send_email_draft

    # Undo window: the message is queued with a delay so "Отменить" is a real
    # option rather than an apology. 0 keeps the old immediate behaviour.
    delay = _resolve_send_delay(payload)
    task = (
        send_email_draft.apply_async(args=[str(draft.id)], countdown=delay)
        if delay > 0
        else send_email_draft.delay(str(draft.id))
    )
    # Do NOT set executed here — the Celery task sets it after the SMTP send
    # actually succeeds; setting it now makes the task bail with "already_sent".
    d = dict(draft.draft_data or {})
    d["status"] = "queued"
    d["task_id"] = task.id
    d["risk_flags"] = [
        {"code": f.code, "severity": f.severity, "message": f.message,
         "can_override": f.can_override}
        for f in detected
    ]
    if delay > 0:
        d["send_after"] = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat()
    draft.draft_data = d
    flag_modified(draft, "draft_data")
    await db.commit()

    await log_action(
        db, action="email.send", entity_type="email", entity_id=draft.id,
        details={"to": payload.to_addresses, "subject": payload.subject, "by": user.sub},
    )
    try:
        from app.api.email_contacts import remember_recipients

        await remember_recipients(
            db, owner_sub=user.sub,
            addresses=list(payload.to_addresses) + list(payload.cc_addresses),
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        # Was a bare `pass`: the address book silently stopped filling up and
        # nothing anywhere said why.
        logger.warning("email_remember_recipients_failed", error=str(exc))
    logger.info(
        "email_user_send_queued", draft_id=str(draft.id), user=user.sub, delay=delay,
    )
    return EmailSendResult(
        task_id=task.id, draft_id=draft.id, status="queued",
        warnings=[
            {"code": f.code, "message": f.message} for f in detected if not f.blocking
        ],
        undo_seconds=delay,
    )


@router.post("/drafts/{draft_id}/cancel-send")
async def cancel_send(
    draft_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Recall a message that is queued but not yet handed to SMTP (Ф4).

    Cancellation is a flag on the draft rather than a Celery revoke: revoke is
    best-effort and races the worker, while the send task checks this flag
    inside the same row lock it already takes, so "отменено" and "уже ушло" can
    never both be true.
    """
    from sqlalchemy.orm.attributes import flag_modified

    draft = await db.get(DraftAction, draft_id)
    if not draft or not await may_access_draft(db, user, draft.draft_data):
        raise HTTPException(404, "Draft not found")

    data = dict(draft.draft_data or {})
    if draft.executed or data.get("status") in ("sent", "sent_mock"):
        raise HTTPException(409, "Письмо уже отправлено — отменить нельзя")

    data["status"] = "draft"
    data["cancelled"] = True
    data["cancelled_by"] = user.sub
    data.pop("send_after", None)
    draft.draft_data = data
    flag_modified(draft, "draft_data")
    await log_action(
        db, action="email.send_cancelled", entity_type="email", entity_id=draft.id,
        details={"by": user.sub},
    )
    await db.commit()
    logger.info("email_send_cancelled", draft_id=str(draft_id), by=user.sub)
    return {"status": "cancelled", "draft_id": str(draft_id)}


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
    progress: list[str] = []


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

    def _progress() -> list[str]:
        try:
            from app.utils.redis_client import get_async_redis  # noqa: F401
            from app.utils.redis_client import get_sync_redis

            return [
                x.decode() if isinstance(x, bytes) else str(x)
                for x in get_sync_redis().lrange(f"email:compose_progress:{task_id}", 0, -1)
            ]
        except Exception:  # noqa: BLE001
            return []

    r = AsyncResult(task_id, app=celery_app)
    if not r.ready():
        return ComposeAssistPoll(status="pending", progress=_progress())
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
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.compose — Generate a draft from an intent + context.

    Creates a DraftAction (not sent). The agent then follows the existing
    risk_check -> send [GATE] path.

    Проверки доступа здесь такие же, как у ручного создания черновика. Их не
    было вовсе: ``X-Acting-User`` использовался только как ярлык владельца, а
    ни ящик-отправитель, ни читаемость треда-контекста, ни доступность
    вложений никто не проверял — при том, что ``generate_draft_body``
    подтягивает в тело содержимое указанного треда.
    """
    from app.domain.email_compose import ComposeContext, generate_draft_body
    from app.domain.email_send import create_reply_draft

    acting_sub = user.sub
    for_agent = request_is_agent(request)
    if payload.mailbox and not await may_write_mailbox(db, user, payload.mailbox):
        raise HTTPException(403, "Нет доступа для отправки из этого ящика")
    if payload.thread_id:
        thread = await db.get(EmailThread, payload.thread_id)
        if thread is None or not await may_read_mailbox(
            db, user, thread.mailbox, for_agent=for_agent
        ):
            raise HTTPException(404, "Thread not found")
    await _assert_attachments_usable(db, user, payload.attachment_ids)

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
        acting_user_sub=acting_sub,
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
        owner_sub=acting_sub,
    )
    await db.commit()
    await db.refresh(draft)
    logger.info("email_agent_draft_generated", draft_id=str(draft.id))
    return _draft_to_out(draft)


# ── email.risk_check ───────────────────────────────────────────────────────


@router.post("/drafts/{draft_id}/risk-check", response_model=RiskCheckResponse)
async def risk_check(
    draft_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.risk_check — Check email draft for risks before sending."""
    result = await db.execute(
        select(DraftAction).where(DraftAction.id == draft_id)
    )
    draft = result.scalar_one_or_none()
    if not draft or not await may_access_draft(db, user, draft.draft_data):
        raise HTTPException(404, "Draft not found")

    is_safe, flags = await _run_risk_check(db, draft)
    return RiskCheckResponse(draft_id=draft.id, is_safe=is_safe, flags=flags)


async def _run_risk_check(db: AsyncSession, draft) -> tuple[bool, list["RiskFlag"]]:
    """Прогнать детекторы по черновику и заморозить результат на его содержимом.

    Вынесено из эндпоинта, потому что этим же занимается ``send``, когда
    проверки ещё не было: раньше он просто отказывал, и вызывающий узнавал о
    порядке действий из ошибки. Проверка при этом не пропускается — она
    выполняется, и её вердикт решает, уйдёт письмо или нет.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.domain.email_risk import blocking_flags, evaluate_draft
    from app.domain.email_send import draft_content_digest

    data = draft.draft_data or {}
    detected = await evaluate_draft(db, data)
    flags: list[RiskFlag] = [
        RiskFlag(
            code=f.code, severity=f.severity, message=f.message,
            can_override=f.can_override,
        )
        for f in detected
    ]

    # "Safe" now means "nothing that blocks", not "nothing of severity error":
    # the old wording called a draft unsafe over an advisory flag and, at the
    # same time, let the one error-level detector through (see email_risk).
    is_safe = not blocking_flags(detected)

    data["status"] = "risk_checked"
    data["risk_flags"] = [f.model_dump() for f in flags]
    # Freeze exactly what was inspected: send refuses to go out with content
    # that changed after the check (and therefore after any human approval).
    data["content_digest"] = draft_content_digest(data)
    data["risk_checked_digest"] = data["content_digest"]
    draft.draft_data = data
    flag_modified(draft, "draft_data")
    await db.commit()
    return is_safe, flags


# ── email.send ─────────────────────────────────────────────────────────────


class SendDraftRequest(BaseModel):
    """``expected_digest`` is the ``content_digest`` the caller last saw.

    This endpoint is the agent's approval-gated send path (the human composer
    uses POST /api/email/send instead). Requiring the digest is what makes the
    gate mean something: the approval machinery hashes the call arguments
    (work_planning.tool_call_digest), so with the digest among them a decision
    is bound to a specific letter rather than to a mutable draft id.
    """

    expected_digest: str | None = None
    # Коды блокирующих флагов, которые человек увидел и осознанно принял.
    # Без этого блокирующая проверка была бы стеной без двери: человеческий
    # путь отправки (compose_and_send) такое подтверждение принимает давно.
    acknowledged_risks: list[str] = []


@router.post("/drafts/{draft_id}/send")
async def send_email(
    draft_id: uuid.UUID,
    # Plain model with a default (not `| None`): the body stays optional on the
    # wire, while the declared fields remain introspectable — an Optional[...]
    # annotation hides them from the capability-contract check, which is exactly
    # the "parameter no endpoint accepts" trap that test guards against.
    payload: SendDraftRequest = SendDraftRequest(),
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.send — Send email draft via SMTP (approval gate).

    Authorisation is NOT implied by the approval gate: the gate decides whether
    the agent may perform the action at all, this check decides whether *this
    caller* owns the draft and may send from its mailbox. Before Ф0.1 the
    endpoint had neither — any authenticated user could send any draft,
    including one composed inside a colleague's personal mailbox.
    """
    result = await db.execute(
        select(DraftAction).where(DraftAction.id == draft_id)
    )
    draft = result.scalar_one_or_none()
    if not draft or not await may_access_draft(db, user, draft.draft_data):
        raise HTTPException(404, "Draft not found")

    if draft.executed:
        raise HTTPException(400, "Email already sent")

    data = draft.draft_data or {}
    mailbox = data.get("mailbox")
    if not mailbox:
        # Fail-closed. Раньше пустой ящик означал «разбирайся при отправке»:
        # авторизация ниже пропускала письмо (проверять нечего), а воркер уже
        # после подтверждения выбирал аккаунт сам — человек подтверждал текст,
        # не зная адреса отправителя, а при единственном личном ящике письмо
        # уходило от имени сотрудника.
        raise HTTPException(
            400,
            {
                "error_code": "mailbox_unresolved",
                "message": (
                    "У черновика не указан ящик отправителя. Укажите mailbox "
                    "в черновике — отправлять от произвольного адреса нельзя."
                ),
            },
        )
    if not await may_write_mailbox(db, user, mailbox):
        raise HTTPException(403, "Нет доступа для отправки из этого ящика")

    # Уже в очереди — второй раз не ставим. Раньше это обеспечивал побочный
    # эффект: у поставленного в очередь черновика статус переставал быть
    # "risk_checked", и его отбивала проверка «risk_check обязателен». Защита
    # от дубля не должна держаться на чужой ошибке: в живом инциденте на один
    # черновик пришлось шесть задач отправки. Отмена возвращает статус в
    # "draft", поэтому отменённое письмо отправить снова можно.
    if data.get("status") in ("queued", "sent", "sent_mock"):
        raise HTTPException(
            400,
            {
                "error_code": "already_queued",
                "message": "Письмо уже отправляется или отправлено",
                "status": data.get("status"),
            },
        )

    # Content binding (Ф0.2): what is about to be sent must be what was
    # checked, and what the caller thinks it is approving. Проверяется ДО
    # авто-проверки рисков: свежий прогон перезаписал бы content_digest и
    # подтверждение, данное на другое содержимое, стало бы «совпадающим».
    expected = payload.expected_digest if payload else None
    current_digest = data.get("content_digest")
    if current_digest and not expected:
        # Раньше проверка была `if expected and ...`, то есть привязка
        # подтверждения к тексту письма выполнялась только если вызывающий сам
        # о ней вспомнил. Никто в backend/app/ai этот параметр не подставляет —
        # его должна была передать модель, прочитав описание capability. Защита,
        # которую обходит забывчивость, защитой не является: без digest — отказ
        # с подсказкой, где его взять.
        raise HTTPException(
            400,
            {
                "error_code": "digest_required",
                "message": (
                    "Для отправки нужен expected_digest — content_digest из "
                    "последнего чтения черновика."
                ),
                "content_digest": current_digest,
                "hint": (
                    "Повтори send с expected_digest=content_digest из ответа "
                    "action=draft/get_draft/risk_check."
                ),
            },
        )
    if expected and current_digest and expected != current_digest:
        raise HTTPException(
            409,
            "Черновик изменился после подтверждения — перечитайте его и повторите отправку",
        )

    # Проверка рисков обязательна, но добывать её отдельным вызовом вызывающий
    # не обязан: раньше send просто отказывал, и агент узнавал о нужном порядке
    # действий из ошибки — лишний круг на каждой отправке. Проверка не
    # пропускается: если её не было или содержимое изменилось после неё, она
    # выполняется здесь и её вердикт решает судьбу письма.
    checked = data.get("risk_checked_digest")
    needs_check = (
        data.get("status") not in ("risk_checked", "approved")
        or not checked
        or checked != data.get("content_digest")
    )
    if needs_check:
        is_safe, fresh_flags = await _run_risk_check(db, draft)
        data = draft.draft_data or {}
        logger.info(
            "email_send_ran_risk_check_inline",
            draft_id=str(draft_id), is_safe=is_safe,
            flags=[f.code for f in fresh_flags],
        )

    # Блокирующие флаги — по общей политике (email_risk.BLOCKING_CODES), той
    # же, по которой risk_check считает черновик безопасным. Раньше здесь был
    # свой предикат `severity == "error" and can_override is False`, под
    # который не подходил ни один детектор: письмо с блокирующим флагом
    # уходило, хотя проверка называла его небезопасным.
    from app.domain.email_risk import BLOCKING_CODES

    acknowledged = {c.strip() for c in (payload.acknowledged_risks if payload else [])}
    hit = [f for f in (data.get("risk_flags") or []) if f.get("code") in BLOCKING_CODES]
    blocking = [f for f in hit if f.get("code") not in acknowledged]
    suppressed = sorted({f.get("code") for f in hit if f.get("code") in acknowledged})
    if suppressed:
        # Обход блокирующей проверки — решение человека, и оно должно быть
        # видно в аудите, как и на человеческом пути отправки. Логируем только
        # когда подтверждение действительно что-то сняло: иначе в журнале
        # копились бы «переопределения», ничего не переопределившие.
        await log_action(
            db, action="email.risk_override", entity_type="email", entity_id=draft.id,
            details={"codes": suppressed, "by": user.sub, "path": "agent_send"},
        )
        await db.commit()

    if blocking:
        raise HTTPException(
            400,
            {
                "error_code": "blocked_by_risk",
                "message": blocking[0]["message"],
                "blocked_by": [f.get("code") for f in blocking],
                "flags": blocking,
                "hint": (
                    "Отправка остановлена проверкой рисков. Покажите причину "
                    "человеку; если он подтверждает — повторите с "
                    "acknowledged_risks."
                ),
            },
        )

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

    Every attachment becomes a Document at IMAP ingest time
    (app.tasks.ingest._store_attachment) and, since Ф6.1, is queued for
    classification/extraction right there — provided the mailbox has
    ``auto_process_attachments`` on (and, for a personal mailbox, its owner
    consented via ``sweep_enabled``). Until Ф6.1 this docstring claimed the
    automatic part while nothing in the IMAP path ever called
    ``process_document``; an emailed invoice stayed an un-parsed Document.

    This endpoint remains for what automatic triage does not cover: a
    quarantined extension, a failed/low-confidence classification the user
    wants re-run, a mailbox with automation switched off, or turning a drawing
    attachment into a CAD Drawing record (a separate pipeline from
    Document/invoice extraction).
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


def _content_disposition(kind: str, filename: str) -> str:
    """RFC 6266 / 5987 Content-Disposition.

    Filenames here are usually Cyrillic ("Счёт №123.pdf"). Interpolating them
    straight into the header — what this endpoint did — produces a header
    latin-1 cannot encode, and a quote in the name would let a sender inject
    header parameters. ASCII fallback plus filename* is the correct form.
    """
    from urllib.parse import quote

    ascii_name = (filename or "attachment").encode("ascii", "replace").decode("ascii")
    ascii_name = ascii_name.replace('"', "_").replace("\\", "_").replace("\r", "").replace("\n", "")
    return f'{kind}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename or "attachment")}'


# Types that a browser will happily execute in the origin that served them.
# An emailed .html/.svg opened "inline" from the API origin is a stored XSS with
# the viewer's session cookie attached, so these are always downloaded, never
# rendered — regardless of what the sender labelled them.
_EXECUTABLE_ATTACHMENT_TYPES = {
    "text/html", "application/xhtml+xml", "image/svg+xml",
    "text/xml", "application/xml", "text/xsl", "application/mathml+xml",
}
# The only types worth previewing in place; everything else is a download.
_INLINE_SAFE_TYPES = {
    "application/pdf",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "text/plain",
}


@router.get("/messages/{message_id}/attachments/{filename}/content")
async def get_attachment_content(
    message_id: uuid.UUID,
    filename: str,
    disposition: str = "attachment",
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.get_attachment — Stream the raw bytes of one attachment.

    Downloads by default. ``?disposition=inline`` is honoured only for the
    small allowlist of types that are safe to render (PDF, raster images,
    plain text) — never for anything the browser would execute.
    """
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

    declared = (att.content_type or "").split(";")[0].strip().lower()
    executable = declared in _EXECUTABLE_ATTACHMENT_TYPES
    media_type = "application/octet-stream" if executable else (
        att.content_type or "application/octet-stream"
    )
    inline_ok = disposition == "inline" and not executable and declared in _INLINE_SAFE_TYPES
    return StreamingResponse(
        iter([data]),
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(
                "inline" if inline_ok else "attachment", att.filename
            ),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/messages/{message_id}/attachments/archive")
async def download_all_attachments(
    message_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Ф5.3 — every attachment of one message as a single .zip.

    A letter with eleven specifications is eleven clicks, and the eleventh is
    the one people miss. Inline parts (signature logos) are left out: they are
    not attachments in any sense a person means.
    """
    import io
    import zipfile

    from fastapi.responses import StreamingResponse

    msg = await db.get(EmailMessage, message_id)
    if not msg or not await may_read_mailbox(db, user, msg.mailbox):
        raise HTTPException(404, "Not found")
    rows = (
        await db.execute(
            select(EmailAttachment).where(
                EmailAttachment.message_id == message_id,
                EmailAttachment.is_inline == False,  # noqa: E712
                EmailAttachment.storage_path.isnot(None),
            )
        )
    ).scalars().all()
    if not rows:
        raise HTTPException(404, "Вложений нет")

    from app.storage import download_file

    buf = io.BytesIO()
    missing: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        seen: dict[str, int] = {}
        for att in rows:
            try:
                data = await asyncio.to_thread(download_file, att.storage_path)
            except Exception as exc:  # noqa: BLE001
                # One unreachable object must not cost the user the other ten.
                logger.warning(
                    "email_zip_attachment_missing",
                    attachment_id=str(att.id), error=str(exc),
                )
                missing.append(att.filename)
                continue
            name = att.filename or "attachment"
            if name in seen:
                seen[name] += 1
                stem, _, ext = name.rpartition(".")
                name = f"{stem} ({seen[name]}).{ext}" if stem else f"{name} ({seen[name]})"
            else:
                seen[name] = 0
            zf.writestr(name, data)
        if missing:
            # Silence here would look like the files never existed.
            zf.writestr(
                "НЕ УДАЛОСЬ ПРОЧИТАТЬ.txt",
                "Эти вложения не удалось получить из хранилища:\n"
                + "\n".join(missing),
            )
    if len(missing) == len(rows):
        raise HTTPException(502, "Ни одно вложение не удалось получить из хранилища")
    buf.seek(0)

    stem = (msg.subject or "Вложения").strip()[:60] or "Вложения"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={
            "Content-Disposition": _content_disposition("attachment", f"{stem}.zip"),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/messages/{message_id}/raw")
async def download_message_eml(
    message_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Download one message as .eml — REBUILT from what we stored.

    Deliberately not called "оригинал": the raw RFC822 source is not kept
    anywhere (the parser extracts fields and discards the bytes), so this is a
    reconstruction — same headers we captured, same bodies, same attachments,
    but not byte-identical to what the server delivered. Claiming otherwise
    would make it useless for the one case people want a raw source for, which
    is arguing about what was actually sent.

    Keeping the true source is a storage decision, not a bug fix — see Ф8.
    """
    from email.message import EmailMessage as MimeMessage
    from fastapi.responses import Response

    msg = await db.get(EmailMessage, message_id)
    if not msg or not await may_read_mailbox(db, user, msg.mailbox):
        raise HTTPException(404, "Not found")

    mime = MimeMessage()
    mime["Subject"] = msg.subject or ""
    mime["From"] = msg.from_address or ""
    mime["To"] = ", ".join(msg.to_addresses or [])
    if msg.cc_addresses:
        mime["Cc"] = ", ".join(msg.cc_addresses)
    if msg.message_id_header:
        mime["Message-ID"] = msg.message_id_header
    if msg.in_reply_to:
        mime["In-Reply-To"] = msg.in_reply_to
    if msg.references:
        mime["References"] = msg.references
    if msg.reply_to:
        mime["Reply-To"] = msg.reply_to
    if msg.sent_at:
        from email.utils import format_datetime

        mime["Date"] = format_datetime(msg.sent_at)
    for key, value in (msg.headers_meta or {}).items():
        if key == "auth" or not isinstance(value, str):
            continue
        mime[key.replace("_", "-").title()] = value
    mime["X-AI-Docs-Reconstructed"] = "yes"

    mime.set_content(msg.body_text or "")
    if msg.body_html:
        mime.add_alternative(msg.body_html, subtype="html")

    attachments = (
        await db.execute(
            select(EmailAttachment).where(EmailAttachment.message_id == message_id)
        )
    ).scalars().all()
    for att in attachments:
        if not att.storage_path:
            continue
        try:
            from app.storage import download_file

            payload = await asyncio.to_thread(download_file, att.storage_path)
        except Exception:  # noqa: BLE001
            continue
        ctype = (att.content_type or "application/octet-stream").split("/")
        mime.add_attachment(
            payload,
            maintype=ctype[0] or "application",
            subtype=ctype[1] if len(ctype) > 1 else "octet-stream",
            filename=att.filename,
        )

    name = f"{(msg.subject or 'message')[:60]}.eml"
    return Response(
        content=mime.as_bytes(),
        media_type="message/rfc822",
        headers={
            "Content-Disposition": _content_disposition("attachment", name),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/messages/{message_id}/attachments/cid/{content_id}/content")
async def get_inline_part(
    message_id: uuid.UUID,
    content_id: str,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Bytes of an inline part referenced from the HTML body by ``cid:``.

    Same authorisation as any other attachment. Only image types are served,
    and always with a restrictive CSP: this is the one attachment path whose
    output is rendered rather than downloaded.
    """
    from fastapi.responses import StreamingResponse

    msg = await db.get(EmailMessage, message_id)
    if not msg or not await may_read_mailbox(db, user, msg.mailbox):
        raise HTTPException(404, "Not found")
    att = (
        await db.execute(
            select(EmailAttachment).where(
                EmailAttachment.message_id == message_id,
                EmailAttachment.content_id == content_id,
            )
        )
    ).scalars().first()
    if not att or not att.storage_path:
        raise HTTPException(404, "Inline part not found")
    declared = (att.content_type or "").split(";")[0].strip().lower()
    if not declared.startswith("image/") or declared == "image/svg+xml":
        raise HTTPException(415, "Только изображения")
    try:
        from app.storage import download_file

        data = await asyncio.to_thread(download_file, att.storage_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Storage unavailable: {exc}")
    return StreamingResponse(
        iter([data]),
        media_type=declared,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=600",
        },
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
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.reply — Draft a reply into an existing thread (correct
    threading headers). Send still goes through the email.send gate.

    Читаемость треда проверяется тем же правилом, что и в get_thread: ответ
    вытягивает переписку в тело черновика, а черновик принадлежит вызывающему
    — без этой проверки ``email.reply`` был способом вынести содержимое чужого
    личного ящика наружу, минуя приватность из email_access.
    """
    from app.domain.email_compose import ComposeContext, generate_draft_body
    from app.domain.email_send import create_reply_draft

    thread = await db.get(EmailThread, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    if not await may_read_mailbox(
        db, user, thread.mailbox, for_agent=request_is_agent(request)
    ):
        raise HTTPException(404, "Thread not found")
    if not await may_write_mailbox(db, user, thread.mailbox):
        raise HTTPException(403, "Нет доступа для отправки из этого ящика")
    await _assert_attachments_usable(db, user, payload.attachment_ids)
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
        acting_user_sub=user.sub,
    )
    # Reply-To wins over From: suppliers routinely send from a no-reply address
    # with Reply-To pointing at the sales desk.
    to = payload.to_addresses or (
        [last_inbound.reply_to or last_inbound.from_address] if last_inbound else []
    )
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
        owner_sub=user.sub,
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
