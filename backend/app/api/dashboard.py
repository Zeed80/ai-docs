"""Dashboard API — counters and unified decision feed."""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import (
    Approval, ApprovalStatus,
    AnomalyCard, AnomalyStatus, AnomalySeverity,
    Document, DocumentStatus,
    EmailMessage,
    QuarantineEntry,
    AuditTimelineEvent,
)

router = APIRouter()


# ── Shared helpers ─────────────────────────────────────────────────────────────

async def _count(db: AsyncSession, q) -> int:
    result = await db.execute(select(func.count()).select_from(q.subquery()))
    return result.scalar() or 0


# ── Unified feed ───────────────────────────────────────────────────────────────

class FeedItem(BaseModel):
    id: uuid.UUID
    type: Literal["approval", "anomaly", "quarantine"]
    priority: Literal["critical", "warning", "info"]
    title: str
    summary: str
    entity_type: str
    entity_id: uuid.UUID
    created_at: datetime
    meta: dict


class FeedResponse(BaseModel):
    items: list[FeedItem]
    total: int


@router.get("/feed", response_model=FeedResponse)
async def decision_feed(db: AsyncSession = Depends(get_db)):
    """Unified queue of items requiring human decision."""
    items: list[FeedItem] = []

    # Pending approvals
    approvals = (await db.execute(
        select(Approval)
        .where(Approval.status == ApprovalStatus.pending)
        .order_by(Approval.created_at.desc())
        .limit(50)
    )).scalars().all()
    for a in approvals:
        items.append(FeedItem(
            id=a.id,
            type="approval",
            priority="critical",
            title=f"Требует согласования: {a.action_type.value}",
            summary=(a.context or {}).get("description", "") or str(a.action_type.value),
            entity_type=a.entity_type,
            entity_id=a.entity_id,
            created_at=a.created_at,
            meta={"action_type": a.action_type.value, "requested_by": a.requested_by},
        ))

    # Open anomalies
    anomalies = (await db.execute(
        select(AnomalyCard)
        .where(AnomalyCard.status == AnomalyStatus.open)
        .order_by(AnomalyCard.created_at.desc())
        .limit(50)
    )).scalars().all()
    for a in anomalies:
        priority = "critical" if a.severity == AnomalySeverity.critical else "warning"
        items.append(FeedItem(
            id=a.id,
            type="anomaly",
            priority=priority,
            title=a.title,
            summary=a.description or "",
            entity_type=a.entity_type,
            entity_id=a.entity_id,
            created_at=a.created_at,
            meta={"anomaly_type": a.anomaly_type.value, "severity": a.severity.value},
        ))

    # Quarantine
    quarantine = (await db.execute(
        select(QuarantineEntry)
        .where(QuarantineEntry.decision.is_(None))
        .order_by(QuarantineEntry.created_at.desc())
        .limit(50)
    )).scalars().all()
    for q in quarantine:
        items.append(FeedItem(
            id=q.id,
            type="quarantine",
            priority="warning",
            title=f"Файл в карантине: {q.original_filename}",
            summary=f"Причина: {q.reason}",
            entity_type="document",
            entity_id=q.document_id,
            created_at=q.created_at,
            meta={"reason": q.reason, "filename": q.original_filename, "mime": q.detected_mime},
        ))

    items.sort(key=lambda x: (0 if x.priority == "critical" else 1, x.created_at), reverse=False)
    items.sort(key=lambda x: x.priority == "critical", reverse=True)

    return FeedResponse(items=items, total=len(items))


# ── Counters ───────────────────────────────────────────────────────────────────

class ActivityItem(BaseModel):
    timestamp: datetime
    entity_type: str
    entity_id: uuid.UUID
    event_type: str
    actor: str | None
    summary: str


class DashboardToday(BaseModel):
    pending_approvals: int
    open_anomalies: int
    documents_needs_review: int
    quarantine_count: int
    unread_emails: int
    recent_activity: list[ActivityItem]


@router.get("/today", response_model=DashboardToday)
async def dashboard_today(db: AsyncSession = Depends(get_db)):
    """Skill: dashboard.today — Read counters and recent activity for today."""
    pending_approvals = await _count(
        db, select(Approval).where(Approval.status == ApprovalStatus.pending)
    )
    open_anomalies = await _count(
        db, select(AnomalyCard).where(AnomalyCard.status == AnomalyStatus.open)
    )
    documents_needs_review = await _count(
        db, select(Document).where(Document.status == DocumentStatus.needs_review)
    )
    quarantine_count = await _count(
        db, select(QuarantineEntry).where(QuarantineEntry.decision.is_(None))
    )
    unread_emails = await _count(
        db, select(EmailMessage).where(EmailMessage.is_inbound == True)
    )

    events = (await db.execute(
        select(AuditTimelineEvent)
        .order_by(AuditTimelineEvent.timestamp.desc())
        .limit(15)
    )).scalars().all()

    return DashboardToday(
        pending_approvals=pending_approvals,
        open_anomalies=open_anomalies,
        documents_needs_review=documents_needs_review,
        quarantine_count=quarantine_count,
        unread_emails=unread_emails,
        recent_activity=[
            ActivityItem(
                timestamp=e.timestamp,
                entity_type=e.entity_type,
                entity_id=e.entity_id,
                event_type=e.event_type,
                actor=e.actor,
                summary=e.summary,
            )
            for e in events
        ],
    )
