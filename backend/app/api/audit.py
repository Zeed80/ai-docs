"""Audit API — skills: audit.list, audit.timeline, audit.filter, audit.export"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import AuditLog, AuditTimelineEvent
from app.auth.jwt import get_current_user, require_role
from app.auth.models import UserInfo, UserRole

router = APIRouter()


class AuditLogOut(BaseModel):
    id: uuid.UUID
    timestamp: datetime
    user_id: str | None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    details: dict | None
    ip_address: str | None

    model_config = {"from_attributes": True}


class AuditLogListResponse(BaseModel):
    items: list[AuditLogOut]
    total: int


class AuditTimelineEventOut(BaseModel):
    id: uuid.UUID
    timestamp: datetime
    entity_type: str
    entity_id: uuid.UUID
    event_type: str
    actor: str | None
    summary: str
    details: dict | None

    model_config = {"from_attributes": True}


class AuditTimelineResponse(BaseModel):
    items: list[AuditTimelineEventOut]
    total: int


class AuditExportRow(BaseModel):
    timestamp: str
    user_id: str
    action: str
    entity_type: str
    entity_id: str
    details: str


class AuditExportResponse(BaseModel):
    rows: list[AuditExportRow]
    count: int
    format: str


@router.get("", response_model=AuditLogListResponse)
async def audit_list(
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    user_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    offset: int = 0,
    limit: int = Query(50, le=500),
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_role(UserRole.manager)),
):
    """Skill: audit.list — List audit log entries with optional filters (agent-facing endpoint)."""
    filters = []
    if action:
        filters.append(AuditLog.action == action)
    if entity_type:
        filters.append(AuditLog.entity_type == entity_type)
    if entity_id:
        filters.append(AuditLog.entity_id == entity_id)
    if user_id:
        filters.append(AuditLog.user_id == user_id)
    if since:
        filters.append(AuditLog.timestamp >= since)
    if until:
        filters.append(AuditLog.timestamp <= until)

    base = select(AuditLog)
    if filters:
        base = base.where(and_(*filters))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = (await db.execute(base.order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit))).scalars().all()

    return AuditLogListResponse(items=rows, total=total)


@router.get("/timeline", response_model=AuditTimelineResponse)
async def audit_timeline(
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    event_type: str | None = None,
    actor: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    offset: int = 0,
    limit: int = Query(50, le=500),
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(get_current_user),
):
    """Skill: audit.timeline — Entity-level timeline events (what happened to a document/invoice/approval)."""
    filters = []
    if entity_type:
        filters.append(AuditTimelineEvent.entity_type == entity_type)
    if entity_id:
        filters.append(AuditTimelineEvent.entity_id == entity_id)
    if event_type:
        filters.append(AuditTimelineEvent.event_type == event_type)
    if actor:
        filters.append(AuditTimelineEvent.actor == actor)
    if since:
        filters.append(AuditTimelineEvent.timestamp >= since)
    if until:
        filters.append(AuditTimelineEvent.timestamp <= until)

    base = select(AuditTimelineEvent)
    if filters:
        base = base.where(and_(*filters))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = (await db.execute(base.order_by(AuditTimelineEvent.timestamp.desc()).offset(offset).limit(limit))).scalars().all()

    return AuditTimelineResponse(items=rows, total=total)


@router.get("/filter", response_model=AuditLogListResponse)
async def audit_filter(
    actions: list[str] = Query(default=[]),
    entity_types: list[str] = Query(default=[]),
    user_ids: list[str] = Query(default=[]),
    since: datetime | None = None,
    until: datetime | None = None,
    offset: int = 0,
    limit: int = Query(50, le=500),
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_role(UserRole.manager)),
):
    """Skill: audit.filter — Batch-filter audit logs by multiple values (OR within each field)."""
    from sqlalchemy import or_ as sql_or

    filters = []
    if actions:
        filters.append(sql_or(*[AuditLog.action == a for a in actions]))
    if entity_types:
        filters.append(sql_or(*[AuditLog.entity_type == et for et in entity_types]))
    if user_ids:
        filters.append(sql_or(*[AuditLog.user_id == u for u in user_ids]))
    if since:
        filters.append(AuditLog.timestamp >= since)
    if until:
        filters.append(AuditLog.timestamp <= until)

    base = select(AuditLog)
    if filters:
        base = base.where(and_(*filters))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = (await db.execute(base.order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit))).scalars().all()

    return AuditLogListResponse(items=rows, total=total)


@router.get("/export", response_model=AuditExportResponse)
async def audit_export(
    fmt: Literal["csv", "json"] = Query("json", alias="format"),
    entity_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(1000, le=10000),
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_role(UserRole.admin)),
):
    """Skill: audit.export — Export audit log for compliance (CSV or JSON, admin only)."""
    filters = []
    if entity_type:
        filters.append(AuditLog.entity_type == entity_type)
    if since:
        filters.append(AuditLog.timestamp >= since)
    if until:
        filters.append(AuditLog.timestamp <= until)

    base = select(AuditLog)
    if filters:
        base = base.where(and_(*filters))

    rows = (await db.execute(base.order_by(AuditLog.timestamp.asc()).limit(limit))).scalars().all()

    export_rows = [
        AuditExportRow(
            timestamp=r.timestamp.isoformat(),
            user_id=r.user_id or "",
            action=r.action,
            entity_type=r.entity_type,
            entity_id=str(r.entity_id) if r.entity_id else "",
            details=str(r.details or {}),
        )
        for r in rows
    ]

    return AuditExportResponse(rows=export_rows, count=len(export_rows), format=fmt)
