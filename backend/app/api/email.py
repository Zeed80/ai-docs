"""Email API — skills: email.fetch_new, email.read, email.search,
email.draft, email.style_match, email.risk_check, email.send, email.suggest_template"""

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.db.models import EmailMessage, EmailThread, Party, DraftAction
from app.domain.email import (
    EmailDraftCreate,
    EmailDraftOut,
    EmailFetchRequest,
    EmailFetchResponse,
    EmailMessageOut,
    EmailSearchRequest,
    EmailSearchResponse,
    EmailThreadOut,
    RiskCheckResponse,
    RiskFlag,
    StyleAnalyzeRequest,
    StyleAnalyzeResponse,
    TemplateSuggestRequest,
    TemplateSuggestResponse,
    EmailTemplate,
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
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.search — Search emails by query, supplier, or address."""
    query = select(EmailMessage)

    if payload.query:
        query = query.where(
            or_(
                EmailMessage.subject.ilike(f"%{payload.query}%"),
                EmailMessage.body_text.ilike(f"%{payload.query}%"),
                EmailMessage.from_address.ilike(f"%{payload.query}%"),
            )
        )
    if payload.email_address:
        query = query.where(
            or_(
                EmailMessage.from_address.ilike(f"%{payload.email_address}%"),
            )
        )
    if payload.mailbox:
        query = query.where(EmailMessage.mailbox == payload.mailbox)

    # If supplier_id, find their email and filter
    if payload.supplier_id:
        party_result = await db.execute(
            select(Party).where(Party.id == payload.supplier_id)
        )
        party = party_result.scalar_one_or_none()
        if party and party.contact_email:
            query = query.where(
                EmailMessage.from_address.ilike(f"%{party.contact_email}%")
            )

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(EmailMessage.received_at.desc()).limit(payload.limit)
    result = await db.execute(query)
    messages = result.scalars().all()

    return EmailSearchResponse(results=messages, total=total)


# ── Thread viewer ──────────────────────────────────────────────────────────


@router.get("/threads", response_model=list[EmailThreadOut])
async def list_threads(
    mailbox: str | None = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.list_threads — List email threads."""
    query = select(EmailThread).options(selectinload(EmailThread.messages))
    if mailbox:
        query = query.where(EmailThread.mailbox == mailbox)
    query = query.order_by(EmailThread.last_message_at.desc()).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/threads/{thread_id}", response_model=EmailThreadOut)
async def get_thread(
    thread_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.get_thread — Get thread with all messages."""
    result = await db.execute(
        select(EmailThread)
        .where(EmailThread.id == thread_id)
        .options(selectinload(EmailThread.messages))
    )
    thread = result.scalar_one_or_none()
    if not thread:
        raise HTTPException(404, "Thread not found")
    return thread


# ── email.draft ────────────────────────────────────────────────────────────


@router.post("/drafts", response_model=EmailDraftOut)
async def create_draft(
    payload: EmailDraftCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.draft — Create email draft."""
    draft = DraftAction(
        action_type="email.send",
        entity_type="email",
        draft_data={
            "to_addresses": payload.to_addresses,
            "cc_addresses": payload.cc_addresses,
            "subject": payload.subject,
            "body_html": payload.body_html,
            "body_text": payload.body_text,
            "thread_id": str(payload.thread_id) if payload.thread_id else None,
            "supplier_id": str(payload.supplier_id) if payload.supplier_id else None,
            "context": payload.context,
            "status": "draft",
            "risk_flags": [],
        },
    )
    db.add(draft)
    await db.commit()
    await db.refresh(draft)

    logger.info("email_draft_created", draft_id=str(draft.id))
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
        subject=data.get("subject", ""),
        body_html=data.get("body_html"),
        body_text=data.get("body_text"),
        thread_id=uuid.UUID(data["thread_id"]) if data.get("thread_id") else None,
        status=data.get("status", "draft"),
        risk_flags=data.get("risk_flags", []),
        created_at=draft.created_at,
    )


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

    # Detector 1: External domain
    to_addrs = data.get("to_addresses", [])
    known_domains = {"company.ru", "company.com"}  # TODO: configure
    for addr in to_addrs:
        domain = addr.split("@")[-1] if "@" in addr else ""
        if domain and domain not in known_domains:
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

    # TODO: actual SMTP sending via Celery task
    # For now, mark as sent
    draft.executed = True
    draft.executed_at = datetime.now(timezone.utc)
    data["status"] = "sent"
    draft.draft_data = data

    await log_action(
        db,
        action="email.send",
        entity_type="email",
        entity_id=draft.id,
        details={"to": data.get("to_addresses"), "subject": data.get("subject")},
    )
    await db.commit()

    logger.info("email_sent", draft_id=str(draft_id))
    return {"status": "sent", "draft_id": str(draft_id)}


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


# ── email.read (must be last — catch-all path) ────────────────────────────


@router.get("/{email_id}", response_model=EmailMessageOut)
async def read_email(
    email_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.read — Read email message with attachments."""
    result = await db.execute(select(EmailMessage).where(EmailMessage.id == email_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Email message not found")
    return msg
