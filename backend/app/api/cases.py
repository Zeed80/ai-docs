"""Work Cases API — cockpit for grouping documents + approvals + audit."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit.service import log_action
from app.auth.jwt import get_current_user
from app.auth.models import UserInfo
from app.domain.access import apply_visibility
from app.db.models import (
    Approval,
    ApprovalStatus,
    AuditTimelineEvent,
    CaseDocument,
    CaseMember,
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
async def create_case(
    body: CaseCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
) -> CaseOut:
    """Create a new work case."""
    actor = current_user.sub
    # Stamp the owning department from the creator so departmental visibility applies.
    from app.domain.org import get_user

    creator = await get_user(db, actor)
    case = WorkCase(
        title=body.title,
        customer=body.customer,
        task_description=body.task_description,
        created_by=actor,
        status="open",
        department_id=creator.department_id if creator else None,
    )
    db.add(case)
    await db.flush()

    # Creator is the owning member.
    db.add(CaseMember(case_id=case.id, user_sub=actor, role="owner", added_by=actor))

    db.add(AuditTimelineEvent(
        entity_type="case",
        entity_id=case.id,
        event_type="case_created",
        actor=actor,
        summary=f"Кейс создан: {body.title}",
        details={"customer": body.customer},
    ))
    await log_action(db, action="case.create", entity_type="case", entity_id=case.id,
                     user_id=actor, details={"title": body.title})
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
    current_user: UserInfo = Depends(get_current_user),
) -> dict:
    """List work cases."""
    stmt = select(WorkCase).order_by(WorkCase.created_at.desc())
    if status:
        stmt = stmt.where(WorkCase.status == status)
    # Row-level visibility: owner (created_by) + department subtree, OR explicit
    # case membership. Managers/admins are unrestricted (clause is None).
    from sqlalchemy import or_ as _or

    from app.domain.access import visibility_filter

    clause = await visibility_filter(
        db, current_user,
        owner_col=WorkCase.created_by, department_col=WorkCase.department_id,
    )
    if clause is not None:
        member_cases = select(CaseMember.case_id).where(CaseMember.user_sub == current_user.sub)
        stmt = stmt.where(_or(clause, WorkCase.id.in_(member_cases)))
    stmt = stmt.limit(limit).offset(offset)
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
    current_user: UserInfo = Depends(get_current_user),
) -> dict:
    """Add a document to a case."""
    case = await db.get(WorkCase, case_id)
    if not case:
        raise HTTPException(404, detail="Case not found")

    doc = await db.get(Document, body.document_id)
    if not doc:
        raise HTTPException(404, detail="Document not found")

    existing = await db.execute(
        select(CaseDocument).where(
            CaseDocument.case_id == case_id,
            CaseDocument.document_id == body.document_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, detail="Document already in case")

    actor = current_user.sub
    cd = CaseDocument(case_id=case_id, document_id=body.document_id, added_by=actor)
    db.add(cd)

    doc_status = doc.status.value if hasattr(doc.status, "value") else str(doc.status)
    event_type = f"document_{doc_status}" if doc_status in ("suspicious", "quarantined") else "document_added"
    db.add(AuditTimelineEvent(
        entity_type="case",
        entity_id=case_id,
        event_type=event_type,
        actor=actor,
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
    current_user: UserInfo = Depends(get_current_user),
) -> dict:
    """Approve or reject a case approval gate. Requires manager or admin role."""
    from app.auth.models import UserRole
    if UserRole.admin not in current_user.roles and UserRole.manager not in current_user.roles:
        raise HTTPException(status_code=403, detail="Requires manager or admin role")

    approval = await db.get(Approval, approval_id)
    if not approval or approval.entity_id != case_id:
        raise HTTPException(404, detail="Approval not found for this case")
    if approval.status != ApprovalStatus.pending:
        raise HTTPException(422, detail="Approval already decided")

    approved = bool(body.get("approved", True))
    comment = body.get("comment")
    actor = current_user.sub

    approval.status = ApprovalStatus.approved if approved else ApprovalStatus.rejected
    approval.decided_by = actor
    approval.decided_at = datetime.now(UTC)
    approval.decision_comment = comment

    event_type = "approval_gate_approved" if approved else "approval_gate_rejected"
    db.add(AuditTimelineEvent(
        entity_type="case",
        entity_id=case_id,
        event_type=event_type,
        actor=actor,
        summary=f"Решение по approval: {'одобрено' if approved else 'отклонено'}",
        details={"approval_id": str(approval_id), "comment": comment},
    ))
    await log_action(db, action="case.approval_decide", entity_type="case", entity_id=case_id,
                     user_id=actor, details={"approval_id": str(approval_id), "approved": approved})
    await db.commit()
    return {"ok": True, "status": approval.status.value, "event_type": event_type}


@router.patch("/{case_id}")
async def update_case(
    case_id: uuid.UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
) -> CaseOut:
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


# ── Members (multi-user collaboration) ─────────────────────────────────────────


class CaseMemberIn(BaseModel):
    user_sub: str
    role: str = "collaborator"  # owner | collaborator | watcher


class CaseMemberOut(BaseModel):
    user_sub: str
    role: str
    added_by: str | None = None

    model_config = {"from_attributes": True}


@router.get("/{case_id}/members", response_model=list[CaseMemberOut])
async def list_case_members(
    case_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
) -> list[CaseMemberOut]:
    await _get_case(db, case_id)
    rows = (
        await db.execute(select(CaseMember).where(CaseMember.case_id == case_id))
    ).scalars().all()
    return [CaseMemberOut.model_validate(m) for m in rows]


@router.post("/{case_id}/members", response_model=CaseMemberOut, status_code=201)
async def add_case_member(
    case_id: uuid.UUID,
    body: CaseMemberIn,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
) -> CaseMemberOut:
    case = await _get_case(db, case_id)
    if body.role not in {"owner", "collaborator", "watcher"}:
        raise HTTPException(422, detail="Invalid member role")

    existing = (
        await db.execute(
            select(CaseMember).where(
                CaseMember.case_id == case_id, CaseMember.user_sub == body.user_sub
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.role = body.role
        member = existing
    else:
        member = CaseMember(
            case_id=case_id, user_sub=body.user_sub, role=body.role, added_by=current_user.sub
        )
        db.add(member)

    db.add(AuditTimelineEvent(
        entity_type="case", entity_id=case_id, event_type="member_added",
        actor=current_user.sub,
        summary=f"Участник добавлен: {body.user_sub} ({body.role})",
    ))
    # Notify the added collaborator.
    from app.db.models import NotificationType
    from app.services.notifications import create_notification

    if body.user_sub != current_user.sub:
        await create_notification(
            db=db, user_sub=body.user_sub, type=NotificationType.system,
            title="Вас добавили в кейс",
            body=case.title,
            entity_type="case", entity_id=case_id, action_url=f"/cases/{case_id}",
        )
    await db.commit()
    await db.refresh(member)
    return CaseMemberOut.model_validate(member)


@router.delete("/{case_id}/members/{user_sub}", status_code=204)
async def remove_case_member(
    case_id: uuid.UUID,
    user_sub: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
) -> None:
    member = (
        await db.execute(
            select(CaseMember).where(
                CaseMember.case_id == case_id, CaseMember.user_sub == user_sub
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, detail="Member not found")
    await db.delete(member)
    db.add(AuditTimelineEvent(
        entity_type="case", entity_id=case_id, event_type="member_removed",
        actor=current_user.sub, summary=f"Участник удалён: {user_sub}",
    ))
    await db.commit()
