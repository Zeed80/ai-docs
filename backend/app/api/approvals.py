"""Approvals API — skills: approval.request, approval.status, approval.list_pending"""

import uuid
from datetime import datetime, timezone
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import Approval, ApprovalStatus, ApprovalActionType
from app.domain.approvals import (
    ApprovalChainCreate,
    ApprovalChainOut,
    ApprovalCreate,
    ApprovalDecision,
    ApprovalListResponse,
    ApprovalOut,
)
from app.audit.service import log_action
from app.auth.jwt import get_current_user, require_role
from app.auth.models import UserInfo, UserRole

router = APIRouter()
logger = structlog.get_logger()


class ApprovalDelegation(BaseModel):
    delegate_to: str
    reason: str | None = None


class BulkDecide(BaseModel):
    approval_ids: list[uuid.UUID]
    status: Literal["approved", "rejected"]
    comment: str | None = None


class BulkDecideResponse(BaseModel):
    processed: int
    failed: int


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

    # Notify the assigned user
    if payload.assigned_to:
        from app.services.notifications import create_notification
        from app.db.models import NotificationType
        await create_notification(
            db=db,
            user_sub=payload.assigned_to,
            type=NotificationType.approval_assigned,
            title="Новое согласование",
            body=f"Требуется ваше решение: {payload.action_type.value}",
            entity_type="approval",
            entity_id=approval.id,
            action_url=f"/approvals?id={approval.id}",
        )

    await db.commit()
    await db.refresh(approval)
    logger.info("approval_requested", approval_id=str(approval.id), action=payload.action_type.value)
    return approval


# NOTE: /pending MUST be before /{approval_id} to avoid route conflict
@router.get("/pending", response_model=ApprovalListResponse)
async def list_pending_approvals(
    action_type: ApprovalActionType | None = None,
    chain_root_id: uuid.UUID | None = None,
    offset: int = 0,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: approval.list_pending — List pending approvals (excludes dormant chain steps)."""
    # Exclude chain steps that are not yet active (assigned_to is None means dormant)
    query = select(Approval).where(
        Approval.status == ApprovalStatus.pending,
        Approval.assigned_to.isnot(None),
    )
    if action_type:
        query = query.where(Approval.action_type == action_type)
    if chain_root_id:
        query = query.where(Approval.chain_root_id == chain_root_id)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    query = query.order_by(Approval.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    items = result.scalars().all()

    return ApprovalListResponse(items=items, total=total)


@router.post("/chain", response_model=ApprovalChainOut, status_code=201)
async def create_approval_chain(
    payload: ApprovalChainCreate,
    db: AsyncSession = Depends(get_db),
) -> ApprovalChainOut:
    """Create a sequential approval chain (step 1 active, steps 2..N dormant until previous approved)."""
    from app.services.notifications import create_notification
    from app.db.models import NotificationType

    # Create step 0 (active — assigned_to set, assigned to first approver)
    root = Approval(
        action_type=payload.action_type,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        requested_by=payload.requested_by,
        assigned_to=payload.steps[0].assigned_to,
        context=payload.context,
        expires_at=payload.expires_at,
        chain_order=0,
    )
    db.add(root)
    await db.flush()

    # Mark root as the chain root
    root.chain_root_id = root.id
    await db.flush()

    all_steps: list[Approval] = [root]

    # Create dormant steps 1..N (assigned_to=None until activated)
    for i, step in enumerate(payload.steps[1:], start=1):
        dormant = Approval(
            action_type=payload.action_type,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            requested_by=payload.requested_by,
            assigned_to=None,          # dormant — not yet visible
            context=payload.context,
            expires_at=payload.expires_at,
            chain_root_id=root.id,
            chain_order=i,
        )
        db.add(dormant)
        all_steps.append(dormant)

    await db.commit()

    # Notify first approver
    await create_notification(
        db=db,
        user_sub=payload.steps[0].assigned_to,
        type=NotificationType.approval_assigned,
        title="Новое согласование (цепочка)",
        body=f"Шаг 1 из {len(payload.steps)}: {payload.action_type.value}",
        entity_type="approval",
        entity_id=root.id,
        action_url=f"/approvals?id={root.id}",
    )
    await db.commit()

    for step in all_steps:
        await db.refresh(step)

    logger.info(
        "approval_chain_created",
        root_id=str(root.id),
        steps=len(all_steps),
        action=payload.action_type.value,
    )
    return ApprovalChainOut(
        chain_root_id=root.id,
        steps=all_steps,
        total_steps=len(all_steps),
    )


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
    # Notify the requester about the decision
    if approval.requested_by and approval.requested_by != "sveta":
        from app.services.notifications import create_notification
        from app.db.models import NotificationType
        status_label = "одобрено" if payload.status == ApprovalStatus.approved else "отклонено"
        await create_notification(
            db=db,
            user_sub=approval.requested_by,
            type=NotificationType.approval_decided,
            title=f"Согласование {status_label}",
            body=payload.comment or f"Решение: {status_label}",
            entity_type="approval",
            entity_id=approval.id,
            action_url=f"/approvals?id={approval.id}",
        )

    await db.commit()
    await db.refresh(approval)
    logger.info("approval_decided", approval_id=str(approval_id), status=payload.status.value)

    # Execute the underlying action after approval
    await _execute_approved_action(approval, db)

    return approval


@router.post("/{approval_id}/delegate", response_model=ApprovalOut)
async def delegate_approval(
    approval_id: uuid.UUID,
    payload: ApprovalDelegation,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> ApprovalOut:
    """Delegate a pending approval to another user."""
    result = await db.execute(select(Approval).where(Approval.id == approval_id))
    approval = result.scalar_one_or_none()
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != ApprovalStatus.pending:
        raise HTTPException(status_code=400, detail=f"Approval already {approval.status.value}")

    is_owner = approval.assigned_to == user.sub
    is_admin = UserRole.admin in user.roles
    if not is_owner and not is_admin:
        from fastapi import status as http_status
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Only the assigned user or admin can delegate",
        )

    approval.status = ApprovalStatus.delegated
    approval.delegated_to = payload.delegate_to

    new_approval = Approval(
        action_type=approval.action_type,
        entity_type=approval.entity_type,
        entity_id=approval.entity_id,
        requested_by=approval.requested_by,
        assigned_to=payload.delegate_to,
        context=approval.context,
        expires_at=approval.expires_at,
    )
    db.add(new_approval)

    await log_action(
        db,
        action="approval.delegated",
        entity_type="approval",
        entity_id=approval.id,
        user_id=user.sub,
        details={"delegate_to": payload.delegate_to, "reason": payload.reason},
    )

    # Notify the new delegate
    from app.services.notifications import create_notification
    from app.db.models import NotificationType
    await create_notification(
        db=db,
        user_sub=payload.delegate_to,
        type=NotificationType.handover,
        title="Согласование передано вам",
        body=payload.reason or "Запрос на согласование делегирован",
        entity_type="approval",
        entity_id=new_approval.id,
        action_url=f"/approvals?id={new_approval.id}",
    )

    await db.commit()
    await db.refresh(approval)
    logger.info("approval_delegated", approval_id=str(approval_id), delegate_to=payload.delegate_to)
    return approval


@router.post("/bulk-decide", response_model=BulkDecideResponse)
async def bulk_decide_approvals(
    payload: BulkDecide,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(require_role(UserRole.manager)),
) -> BulkDecideResponse:
    """Approve or reject multiple pending approvals in one call."""
    processed = 0
    failed = 0
    decision_status = ApprovalStatus(payload.status)

    for approval_id in payload.approval_ids:
        try:
            result = await db.execute(select(Approval).where(Approval.id == approval_id))
            approval = result.scalar_one_or_none()
            if not approval or approval.status != ApprovalStatus.pending:
                failed += 1
                continue

            approval.status = decision_status
            approval.decision_comment = payload.comment
            approval.decided_by = user.sub
            approval.decided_at = datetime.now(timezone.utc)

            await log_action(
                db,
                action=f"approval.{payload.status}",
                entity_type="approval",
                entity_id=approval.id,
                user_id=user.sub,
                details={"bulk": True, "comment": payload.comment},
            )
            processed += 1
        except Exception as exc:
            logger.error("bulk_decide_item_failed", approval_id=str(approval_id), error=str(exc))
            failed += 1

    await db.commit()
    logger.info("bulk_decide_completed", processed=processed, failed=failed)
    return BulkDecideResponse(processed=processed, failed=failed)


async def _execute_approved_action(approval: Approval, db: AsyncSession) -> None:
    """After an approval decision, advance chain or execute domain action."""
    if approval.status != ApprovalStatus.approved:
        return

    # ── Chain advancement ────────────────────────────────────────────────────
    if approval.chain_root_id is not None and approval.chain_order is not None:
        next_order = approval.chain_order + 1
        next_result = await db.execute(
            select(Approval).where(
                Approval.chain_root_id == approval.chain_root_id,
                Approval.chain_order == next_order,
            )
        )
        next_step = next_result.scalar_one_or_none()
        if next_step is not None:
            # Find the original assigned_to from the chain creation context
            # stored in the root's context or derived from domain
            # We activate the next step by looking at its existing assigned_to
            # (set during chain creation) or fallback to first available manager
            if next_step.assigned_to is None:
                # Fallback: assign to first active manager/admin
                from app.db.models import User
                mgr_result = await db.execute(
                    select(User).where(
                        User.is_active == True,  # noqa: E712
                        User.role.in_(["manager", "admin"]),
                    ).limit(1)
                )
                mgr = mgr_result.scalar_one_or_none()
                if mgr:
                    next_step.assigned_to = mgr.sub

            # Count total steps in chain for notification
            count_result = await db.execute(
                select(func.count(Approval.id)).where(
                    Approval.chain_root_id == approval.chain_root_id
                )
            )
            total_steps = count_result.scalar() or next_order + 1

            await db.commit()

            if next_step.assigned_to:
                from app.services.notifications import create_notification
                from app.db.models import NotificationType
                await create_notification(
                    db=db,
                    user_sub=next_step.assigned_to,
                    type=NotificationType.approval_assigned,
                    title="Согласование: следующий шаг",
                    body=f"Шаг {next_order + 1} из {total_steps}: {next_step.action_type.value}",
                    entity_type="approval",
                    entity_id=next_step.id,
                    action_url=f"/approvals?id={next_step.id}",
                )
                await db.commit()

            logger.info(
                "approval_chain_advanced",
                chain_root=str(approval.chain_root_id),
                next_step=str(next_step.id),
                step_order=next_order,
            )
            return  # Do NOT execute domain action yet — chain continues

    # ── Domain action execution (only when chain is complete or not a chain) ──
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
