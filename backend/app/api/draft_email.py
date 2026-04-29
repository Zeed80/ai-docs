"""Draft Email API — AI-generated email drafts with risk-check and approval gate."""

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import (
    DraftEmail,
    Approval,
    ApprovalStatus,
    ApprovalActionType,
    AnomalyCard,
    AnomalyStatus,
    Party,
)
from app.audit.service import log_action, add_timeline_event

router = APIRouter()
logger = structlog.get_logger()


class RiskFlag(BaseModel):
    severity: str  # "warning" | "critical"
    message: str


class DraftEmailOut(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID | None
    related_entity_type: str | None
    related_entity_id: uuid.UUID | None
    to_addresses: list
    cc_addresses: list | None
    subject: str
    body_text: str
    body_html: str | None
    risk_flags: list | None
    approval_id: uuid.UUID | None
    status: str
    generated_by: str
    sent_at: datetime | None

    model_config = {"from_attributes": True}


class DraftEmailCreate(BaseModel):
    to_addresses: list[str]
    cc_addresses: list[str] | None = None
    subject: str | None = None
    body_text: str | None = None
    related_entity_type: str | None = None
    related_entity_id: uuid.UUID | None = None
    thread_id: uuid.UUID | None = None
    context: dict | None = None  # passed to ai_router.generate_email


class DraftEmailUpdate(BaseModel):
    to_addresses: list[str] | None = None
    cc_addresses: list[str] | None = None
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None


class DraftEmailListResponse(BaseModel):
    items: list[DraftEmailOut]
    total: int


async def _run_risk_check(
    db: AsyncSession,
    draft: DraftEmail,
) -> list[dict]:
    """Check draft email for risks before sending."""
    flags: list[dict] = []

    # Check: open anomalies on related entity
    if draft.related_entity_id and draft.related_entity_type:
        result = await db.execute(
            select(AnomalyCard).where(
                AnomalyCard.entity_id == draft.related_entity_id,
                AnomalyCard.status == AnomalyStatus.open,
            )
        )
        anomalies = result.scalars().all()
        for a in anomalies:
            flags.append({
                "severity": a.severity.value,
                "message": f"Открытая аномалия: {a.title}",
            })

    # Check: recipient contains bank account-like strings (naive check)
    sensitive_keywords = ["р/с", "к/с", "бик", "инн", "кпп", "счет"]
    body_lower = (draft.body_text or "").lower()
    found = [kw for kw in sensitive_keywords if kw in body_lower]
    if found:
        flags.append({
            "severity": "warning",
            "message": f"Тело письма содержит финансовые реквизиты: {', '.join(found)}",
        })

    return flags


# ── Create draft ───────────────────────────────────────────────────────────


@router.post("", response_model=DraftEmailOut, status_code=201)
async def create_draft_email(
    payload: DraftEmailCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a draft email. If context is provided, AI generates subject + body."""
    subject = payload.subject or ""
    body_text = payload.body_text or ""

    if payload.context and not (subject and body_text):
        try:
            from app.ai.router import ai_router
            generated = await ai_router.generate_email(payload.context)
            subject = subject or generated.get("subject", "")
            body_text = body_text or generated.get("body_text", "")
            body_html = generated.get("body_html")
        except Exception as e:
            logger.warning("email_generation_failed", error=str(e))
            body_html = None
    else:
        body_html = None

    draft = DraftEmail(
        to_addresses=payload.to_addresses,
        cc_addresses=payload.cc_addresses,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        related_entity_type=payload.related_entity_type,
        related_entity_id=payload.related_entity_id,
        thread_id=payload.thread_id,
        status="draft",
        generated_by="sveta" if payload.context else "user",
    )
    db.add(draft)
    await db.flush()

    # Risk check
    flags = await _run_risk_check(db, draft)
    draft.risk_flags = flags if flags else None

    await db.commit()
    await db.refresh(draft)
    return draft


# ── List drafts ────────────────────────────────────────────────────────────


@router.get("", response_model=DraftEmailListResponse)
async def list_draft_emails(
    status: str | None = None,
    related_entity_id: uuid.UUID | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    q = select(DraftEmail)
    if status:
        q = q.where(DraftEmail.status == status)
    if related_entity_id:
        q = q.where(DraftEmail.related_entity_id == related_entity_id)
    q = q.order_by(DraftEmail.created_at.desc())

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (await db.execute(q.offset(offset).limit(limit))).scalars().all()
    return DraftEmailListResponse(items=list(items), total=total)


@router.get("/{draft_id}", response_model=DraftEmailOut)
async def get_draft_email(
    draft_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    draft = await db.get(DraftEmail, draft_id)
    if not draft:
        raise HTTPException(404, "Draft email not found")
    return draft


# ── Update draft ───────────────────────────────────────────────────────────


@router.patch("/{draft_id}", response_model=DraftEmailOut)
async def update_draft_email(
    draft_id: uuid.UUID,
    payload: DraftEmailUpdate,
    db: AsyncSession = Depends(get_db),
):
    draft = await db.get(DraftEmail, draft_id)
    if not draft:
        raise HTTPException(404, "Draft email not found")
    if draft.status not in ("draft",):
        raise HTTPException(400, f"Cannot edit draft in status '{draft.status}'")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(draft, field, value)

    # Re-run risk check after edit
    flags = await _run_risk_check(db, draft)
    draft.risk_flags = flags if flags else None

    await db.commit()
    await db.refresh(draft)
    return draft


# ── Send draft (approval gate) ─────────────────────────────────────────────


@router.post("/{draft_id}/send", response_model=DraftEmailOut)
async def send_draft_email(
    draft_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Submit draft for sending — creates Approval gate (email.send)."""
    draft = await db.get(DraftEmail, draft_id)
    if not draft:
        raise HTTPException(404, "Draft email not found")
    if draft.status != "draft":
        raise HTTPException(400, f"Cannot send draft in status '{draft.status}'")

    # Re-run risk check
    flags = await _run_risk_check(db, draft)
    draft.risk_flags = flags if flags else None

    # Check for critical risks
    critical = [f for f in (flags or []) if f.get("severity") == "critical"]
    if critical:
        raise HTTPException(
            422,
            f"Отправка заблокирована из-за критических рисков: "
            + "; ".join(f["message"] for f in critical),
        )

    # Create approval
    approval = Approval(
        action_type=ApprovalActionType.email_send,
        entity_type="draft_email",
        entity_id=draft.id,
        status=ApprovalStatus.pending,
        requested_by="user",
        context={
            "to": draft.to_addresses,
            "subject": draft.subject,
            "body_preview": (draft.body_text or "")[:300],
        },
    )
    db.add(approval)
    await db.flush()

    draft.approval_id = approval.id
    draft.status = "pending_approval"

    await log_action(
        db, action="draft_email.send_requested",
        entity_type="draft_email", entity_id=draft.id,
        details={"to": draft.to_addresses, "subject": draft.subject},
    )
    await db.commit()
    await db.refresh(draft)
    return draft


# ── Cancel draft ───────────────────────────────────────────────────────────


@router.delete("/{draft_id}", response_model=DraftEmailOut)
async def cancel_draft_email(
    draft_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    draft = await db.get(DraftEmail, draft_id)
    if not draft:
        raise HTTPException(404, "Draft email not found")
    if draft.status == "sent":
        raise HTTPException(400, "Cannot cancel already sent email")

    draft.status = "cancelled"
    await db.commit()
    await db.refresh(draft)
    return draft
