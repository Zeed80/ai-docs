"""Approvals API — skills: approval.request, approval.status, approval.list_pending"""

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import Approval, ApprovalStatus, ApprovalActionType
from app.domain.approvals import (
    ApprovalCreate,
    ApprovalDecision,
    ApprovalListResponse,
    ApprovalOut,
)
from app.audit.service import log_action
from app.auth.jwt import require_role
from app.auth.models import UserInfo, UserRole

router = APIRouter()
logger = structlog.get_logger()


@router.post("", response_model=ApprovalOut, status_code=201)
async def request_approval(
    payload: ApprovalCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: approval.request — Request human approval. Blocks agent."""
    approval = Approval(
        action_type=payload.action_type,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        requested_by=payload.requested_by,
        assigned_to=payload.assigned_to,
        context=payload.context,
        expires_at=payload.expires_at,
    )
    db.add(approval)
    await db.flush()

    await log_action(
        db,
        action="approval.request",
        entity_type="approval",
        entity_id=approval.id,
        details={"action_type": payload.action_type.value},
    )
    await db.commit()
    await db.refresh(approval)
    logger.info("approval_requested", approval_id=str(approval.id), action=payload.action_type.value)
    return approval


# NOTE: /pending MUST be before /{approval_id} to avoid route conflict
@router.get("/pending", response_model=ApprovalListResponse)
async def list_pending_approvals(
    action_type: ApprovalActionType | None = None,
    offset: int = 0,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: approval.list_pending — List pending approvals."""
    query = select(Approval).where(Approval.status == ApprovalStatus.pending)
    if action_type:
        query = query.where(Approval.action_type == action_type)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    query = query.order_by(Approval.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    items = result.scalars().all()

    return ApprovalListResponse(items=items, total=total)


@router.get("/{approval_id}", response_model=ApprovalOut)
async def get_approval_status(
    approval_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: approval.status — Check approval status."""
    result = await db.execute(select(Approval).where(Approval.id == approval_id))
    approval = result.scalar_one_or_none()
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    return approval


@router.post("/{approval_id}/decide", response_model=ApprovalOut)
async def decide_approval(
    approval_id: uuid.UUID,
    payload: ApprovalDecision,
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_role(UserRole.manager)),
):
    """Decide on an approval (approve/reject)."""
    result = await db.execute(select(Approval).where(Approval.id == approval_id))
    approval = result.scalar_one_or_none()
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")

    if approval.status != ApprovalStatus.pending:
        raise HTTPException(status_code=400, detail=f"Approval already {approval.status.value}")

    approval.status = payload.status
    approval.decision_comment = payload.comment
    approval.decided_by = payload.decided_by
    approval.decided_at = datetime.now(timezone.utc)

    await log_action(
        db,
        action=f"approval.{payload.status.value}",
        entity_type="approval",
        entity_id=approval.id,
        user_id=payload.decided_by,
        details={"comment": payload.comment},
    )
    await db.commit()
    await db.refresh(approval)
    logger.info("approval_decided", approval_id=str(approval_id), status=payload.status.value)

    # Execute the underlying action after approval
    await _execute_approved_action(approval, db)

    return approval


async def _execute_approved_action(approval: Approval, db: AsyncSession) -> None:
    """After an approval decision, apply the corresponding domain action."""
    if approval.status != ApprovalStatus.approved:
        return

    from app.db.models import Invoice, InvoiceStatus

    if approval.action_type in (
        ApprovalActionType.invoice_approve,
        ApprovalActionType.invoice_reject,
    ):
        new_status = (
            InvoiceStatus.approved
            if approval.action_type == ApprovalActionType.invoice_approve
            else InvoiceStatus.rejected
        )
        try:
            result = await db.execute(
                select(Invoice).where(Invoice.id == approval.entity_id)
            )
            invoice = result.scalar_one_or_none()
            if invoice:
                invoice.status = new_status
                await db.commit()
                logger.info(
                    "invoice_status_updated",
                    invoice_id=str(approval.entity_id),
                    status=new_status.value,
                )
        except Exception as e:
            logger.error("execute_approved_action_error", error=str(e))
