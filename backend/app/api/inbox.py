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
    kind: str                    # "approval" | "email" | "document" | "anomaly"
    title: str
    subtitle: str | None = None
    at: datetime | None = None
    url: str
    unread: bool = False
    severity: str | None = None
    badge: str | None = None


# Человекочитаемые названия ожидающих решений: коды вроде "email_send" в
# списке «что от меня ждут» ничего не сообщают.
_APPROVAL_TITLES = {
    "email_send": "Отправить письмо",
    "invoice_approve": "Утвердить счёт",
    "anomaly_resolve": "Закрыть аномалию",
    "table_apply_diff": "Применить правки таблицы",
    "agent_tool_call": "Действие агента",
    "payment_mark_paid": "Отметить платёж",
    "supplier_create": "Создать поставщика",
}


class InboxFeed(BaseModel):
    items: list[InboxItem]
    counts: dict


@router.get("", response_model=InboxFeed)
async def inbox_feed(
    kinds: str = Query("approval,email,document,anomaly"),
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
    counts = {"approval": 0, "email": 0, "document": 0, "anomaly": 0}

    if "approval" in wanted:
        # Самое срочное из всего, что ждёт человека, жило на отдельной
        # странице, внутри чата и на экране поручений — но не в общем списке
        # «что от меня нужно». Вернувшись с обеда, человек видел непрочитанные
        # письма и не видел, что агент стоит и ждёт решения.
        from app.db.models import Approval, ApprovalStatus

        pending = (
            await db.execute(
                select(Approval)
                .where(Approval.status == ApprovalStatus.pending)
                .order_by(Approval.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        counts["approval"] = len(pending)
        for row in pending:
            ctx = row.context if isinstance(row.context, dict) else {}
            action = str(
                row.action_type.value
                if hasattr(row.action_type, "value") else row.action_type
            )
            items.append(InboxItem(
                id=str(row.id),
                kind="approval",
                title=str(ctx.get("title") or _APPROVAL_TITLES.get(action, action)),
                subtitle=(
                    str(ctx.get("subtitle"))
                    if ctx.get("subtitle")
                    else (row.requested_by or None)
                ),
                at=row.created_at,
                url=f"/approvals?id={row.id}",
                unread=True,
                severity="critical" if ctx.get("irreversible") else None,
                badge="ждёт решения",
            ))

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
        # Решение, которого ждёт агент, блокирует его работу — оно идёт выше
        # непрочитанного письма и рядом с критической аномалией.
        waiting = item.kind == "approval"
        rank = 0 if critical else (1 if waiting else 2)
        return (rank, -(item.at.timestamp() if item.at else 0))

    items.sort(key=_key)
    return InboxFeed(items=items[:limit], counts=counts)
