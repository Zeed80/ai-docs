"""Work Cases API — cockpit for grouping documents + approvals + audit."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit.service import log_action
from app.db.models import (
    Approval,
    ApprovalStatus,
    AuditTimelineEvent,
    CaseDocument,
    Document,
    WorkCase,
)
from app.db.session import get_db

router = APIRouter(prefix="/api/cases", tags=["cases"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class CaseCreate(BaseModel):
    title: str
    customer: str | None = None
    task_description: str | None = None
    created_by: str = "system"


class CaseOut(BaseModel):
    id: uuid.UUID
    title: str
    customer: str | None
    task_description: str | None
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    documents_count: int = 0

    model_config = {"from_attributes": True}


class CaseDetailOut(CaseOut):
    documents: list[dict] = []
    timeline: list[dict] = []
    approval_gates: list[dict] = []


class AddDocumentRequest(BaseModel):
    document_id: uuid.UUID
    added_by: str = "system"


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_case(db: AsyncSession, case_id: uuid.UUID) -> WorkCase:
    case = await db.get(WorkCase, case_id, options=[selectinload(WorkCase.documents).selectinload(CaseDocument.document)])
    if not case:
        raise HTTPException(404, detail="Case not found")
    return case


async def _build_timeline(db: AsyncSession, case_id: uuid.UUID) -> list[dict]:
    stmt = (
        select(AuditTimelineEvent)
        .where(
            AuditTimelineEvent.entity_type == "case",
            AuditTimelineEvent.entity_id == case_id,
        )
        .order_by(AuditTimelineEvent.timestamp.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "event_type": r.event_type,
            "actor": r.actor,
            "summary": r.summary,
            "details": r.details,
            "timestamp": r.timestamp.isoformat(),
        }
        for r in rows
    ]


async def _build_approval_gates(db: AsyncSession, case_id: uuid.UUID) -> list[dict]:
    stmt = select(Approval).where(
        Approval.entity_type == "case",
        Approval.entity_id == case_id,
    ).order_by(Approval.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "action_type": r.action_type.value if hasattr(r.action_type, "value") else str(r.action_type),
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "requested_by": r.requested_by,
            "context": r.context or {},
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            "decided_by": r.decided_by,
        }
        for r in rows
    ]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("", status_code=201)
async def create_case(body: CaseCreate, db: AsyncSession = Depends(get_db)) -> CaseOut:
    """Create a new work case."""
    case = WorkCase(
        title=body.title,
        customer=body.customer,
        task_description=body.task_description,
        created_by=body.created_by,
        status="open",
    )
    db.add(case)
    await db.flush()

    # Record creation in timeline
    db.add(AuditTimelineEvent(
        entity_type="case",
        entity_id=case.id,
        event_type="case_created",
        actor=body.created_by,
        summary=f"Кейс создан: {body.title}",
        details={"customer": body.customer},
    ))
    await log_action(db, action="case.create", entity_type="case", entity_id=case.id,
                     user_id=body.created_by, details={"title": body.title})
    await db.commit()
    await db.refresh(case)

    result = CaseOut.model_validate(case)
    result.documents_count = 0
    return result


@router.get("")
async def list_cases(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List work cases."""
    stmt = select(WorkCase).order_by(WorkCase.created_at.desc()).limit(limit).offset(offset)
    if status:
        stmt = stmt.where(WorkCase.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    items = []
    for c in rows:
        o = CaseOut.model_validate(c)
        items.append(o)
    return {"items": items, "total": len(items), "offset": offset, "limit": limit}


@router.get("/{case_id}")
async def get_case(case_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> CaseDetailOut:
    """Get case with documents, timeline, and approval gates."""
    case = await _get_case(db, case_id)

    docs = [
        {
            "id": str(cd.document.id),
            "file_name": cd.document.file_name,
            "status": cd.document.status.value if hasattr(cd.document.status, "value") else str(cd.document.status),
            "doc_type": cd.document.doc_type,
            "added_at": cd.added_at.isoformat(),
        }
        for cd in case.documents
    ]

    timeline = await _build_timeline(db, case_id)
    gates = await _build_approval_gates(db, case_id)

    out = CaseDetailOut.model_validate(case)
    out.documents_count = len(docs)
    out.documents = docs
    out.timeline = timeline
    out.approval_gates = gates
    return out


@router.post("/{case_id}/documents", status_code=201)
async def add_document(
    case_id: uuid.UUID,
    body: AddDocumentRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add a document to a case."""
    case = await db.get(WorkCase, case_id)
    if not case:
        raise HTTPException(404, detail="Case not found")

    doc = await db.get(Document, body.document_id)
    if not doc:
        raise HTTPException(404, detail="Document not found")

    # Check for duplicate
    existing = await db.execute(
        select(CaseDocument).where(
            CaseDocument.case_id == case_id,
            CaseDocument.document_id == body.document_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, detail="Document already in case")

    cd = CaseDocument(case_id=case_id, document_id=body.document_id, added_by=body.added_by)
    db.add(cd)

    # Record in timeline — use doc status for event type
    doc_status = doc.status.value if hasattr(doc.status, "value") else str(doc.status)
    event_type = f"document_{doc_status}" if doc_status in ("suspicious", "quarantined") else "document_added"
    db.add(AuditTimelineEvent(
        entity_type="case",
        entity_id=case_id,
        event_type=event_type,
        actor=body.added_by,
        summary=f"Документ добавлен: {doc.file_name}",
        details={"document_id": str(body.document_id), "file_name": doc.file_name, "status": doc_status},
    ))
    await db.commit()

    return {"ok": True, "case_id": str(case_id), "document_id": str(body.document_id), "event_type": event_type}


@router.get("/{case_id}/documents")
async def list_case_documents(case_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """List documents in a case."""
    case = await _get_case(db, case_id)
    docs = [
        {
            "id": str(cd.document.id),
            "file_name": cd.document.file_name,
            "status": cd.document.status.value if hasattr(cd.document.status, "value") else str(cd.document.status),
            "doc_type": cd.document.doc_type,
            "mime_type": cd.document.mime_type,
            "added_at": cd.added_at.isoformat(),
            "added_by": cd.added_by,
        }
        for cd in case.documents
    ]
    return {"items": docs, "total": len(docs)}


@router.get("/{case_id}/audit")
async def get_case_audit(case_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Get audit timeline for a case."""
    await db.get(WorkCase, case_id)  # check exists
    timeline = await _build_timeline(db, case_id)
    return {"items": timeline, "total": len(timeline)}


@router.post("/{case_id}/approvals/{approval_id}/decide")
async def decide_case_approval(
    case_id: uuid.UUID,
    approval_id: uuid.UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve or reject a case approval gate."""
    approval = await db.get(Approval, approval_id)
    if not approval or approval.entity_id != case_id:
        raise HTTPException(404, detail="Approval not found for this case")
    if approval.status != ApprovalStatus.pending:
        raise HTTPException(422, detail="Approval already decided")

    approved = bool(body.get("approved", True))
    decided_by = body.get("decided_by", "user")
    comment = body.get("comment")

    approval.status = ApprovalStatus.approved if approved else ApprovalStatus.rejected
    approval.decided_by = decided_by
    approval.decided_at = datetime.now(UTC)
    approval.decision_comment = comment

    event_type = "approval_gate_approved" if approved else "approval_gate_rejected"
    db.add(AuditTimelineEvent(
        entity_type="case",
        entity_id=case_id,
        event_type=event_type,
        actor=decided_by,
        summary=f"Решение по approval: {'одобрено' if approved else 'отклонено'}",
        details={"approval_id": str(approval_id), "comment": comment},
    ))
    await log_action(db, action="case.approval_decide", entity_type="case", entity_id=case_id,
                     user_id=decided_by, details={"approval_id": str(approval_id), "approved": approved})
    await db.commit()
    return {"ok": True, "status": approval.status.value, "event_type": event_type}


@router.patch("/{case_id}")
async def update_case(case_id: uuid.UUID, body: dict, db: AsyncSession = Depends(get_db)) -> CaseOut:
    """Update case status or description."""
    case = await db.get(WorkCase, case_id)
    if not case:
        raise HTTPException(404, detail="Case not found")
    if "status" in body:
        case.status = body["status"]
    if "title" in body:
        case.title = body["title"]
    if "customer" in body:
        case.customer = body["customer"]
    if "task_description" in body:
        case.task_description = body["task_description"]
    await db.commit()
    await db.refresh(case)
    out = CaseOut.model_validate(case)
    return out
