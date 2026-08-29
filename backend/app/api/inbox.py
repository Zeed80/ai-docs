"""Unified "Входящие" — one feed of things waiting for a person (Ф7.1).

Until now /inbox listed documents and critical anomalies, and mail lived on a
separate screen the mobile app did not even link to. For someone working from a
phone that meant checking two places to find out whether anything needed them.

Merged server-side rather than by three client fetches: only the server can
order the three sources by time correctly, and only it can apply the
personal-mailbox visibility rules while doing so.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.acting import get_effective_user
from app.auth.models import UserInfo
from app.db.session import get_db

router = APIRouter()
logger = structlog.get_logger()


class InboxItem(BaseModel):
    id: str
    kind: str                    # "email" | "document" | "anomaly"
    title: str
    subtitle: str | None = None
    at: datetime | None = None
    url: str
    unread: bool = False
    severity: str | None = None
    badge: str | None = None


class InboxFeed(BaseModel):
    items: list[InboxItem]
    counts: dict


@router.get("", response_model=InboxFeed)
async def inbox_feed(
    kinds: str = Query("email,document,anomaly"),
    limit: int = Query(60, le=200),
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> InboxFeed:
    """Everything waiting for this person, newest first."""
    from app.db.models import (
        AnomalyCard, AnomalyStatus, Document, DocumentStatus, EmailThread,
    )
    from app.domain.email_access import mailbox_filter

    wanted = {k.strip() for k in kinds.split(",") if k.strip()}
    items: list[InboxItem] = []
    counts = {"email": 0, "document": 0, "anomaly": 0}

    if "email" in wanted:
        query = select(EmailThread).where(
            EmailThread.folder == "inbox",
            EmailThread.is_read == False,  # noqa: E712
        )
        # Someone else's personal mailbox must not surface here either.
        scope = await mailbox_filter(db, user, mailbox_col=EmailThread.mailbox)
        if scope is not None:
            query = query.where(scope)
        threads = (
            await db.execute(
                query.order_by(EmailThread.last_message_at.desc().nullslast()).limit(limit)
            )
        ).scalars().all()
        counts["email"] = len(threads)
        for thread in threads:
            items.append(InboxItem(
                id=str(thread.id),
                kind="email",
                title=thread.subject or "(без темы)",
                subtitle=thread.last_snippet,
                at=thread.last_message_at,
                url=f"/email/{thread.id}",
                unread=True,
                badge=thread.mailbox,
            ))

    if "document" in wanted:
        docs = (
            await db.execute(
                select(Document)
                .where(Document.status == DocumentStatus.needs_review)
                .order_by(Document.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        counts["document"] = len(docs)
        for doc in docs:
            items.append(InboxItem(
                id=str(doc.id),
                kind="document",
                title=doc.file_name,
                subtitle=(doc.doc_type.value if doc.doc_type else None),
                at=doc.created_at,
                url=f"/documents/{doc.id}/review",
                badge="из письма" if doc.source_channel == "email" else None,
            ))

    if "anomaly" in wanted:
        anomalies = (
            await db.execute(
                select(AnomalyCard)
                .where(AnomalyCard.status.in_((AnomalyStatus.open, AnomalyStatus.escalated)))
                .order_by(AnomalyCard.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        counts["anomaly"] = len(anomalies)
        for card in anomalies:
            items.append(InboxItem(
                id=str(card.id),
                kind="anomaly",
                title=card.title or "Аномалия",
                subtitle=(
                    card.anomaly_type.value
                    if hasattr(card.anomaly_type, "value") else str(card.anomaly_type)
                ),
                at=card.created_at,
                url=f"/anomalies?id={card.id}",
                severity=(
                    card.severity.value
                    if hasattr(card.severity, "value") else str(card.severity)
                ),
            ))

    # Anomalies first when critical, then everything by time: a price spike
    # from this morning matters more than an unread newsletter from a minute
    # ago, and a feed sorted purely by clock buries it.
    def _key(item: InboxItem):
        critical = item.kind == "anomaly" and (item.severity or "") == "critical"
        return (0 if critical else 1, -(item.at.timestamp() if item.at else 0))

    items.sort(key=_key)
    return InboxFeed(items=items[:limit], counts=counts)
