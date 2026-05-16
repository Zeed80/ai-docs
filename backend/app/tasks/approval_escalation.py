"""Celery periodic task — escalate expired pending approvals to admin/manager."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(name="approval.escalate_expired")
def escalate_expired_approvals() -> dict:
    """
    Runs every 15 minutes via Celery beat.
    Finds approvals WHERE status='pending' AND expires_at < NOW().
    Marks them as 'expired' and creates a new approval assigned to the first
    active admin or manager found in the users table.
    """
    return asyncio.get_event_loop().run_until_complete(_run_escalation())


async def _run_escalation() -> dict:
    from sqlalchemy import select

    from app.db.models import Approval, ApprovalStatus, User
    from app.db.session import _get_session_factory

    escalated = 0
    errors = 0
    now = datetime.now(timezone.utc)

    async with _get_session_factory()() as db:
        # Find first active admin or manager for reassignment
        manager_result = await db.execute(
            select(User)
            .where(User.is_active == True, User.role.in_(["admin", "manager"]))  # noqa: E712
            .order_by(User.role)  # admin < manager alphabetically → admin first
            .limit(1)
        )
        escalation_target = manager_result.scalar_one_or_none()

        # Find expired pending approvals
        result = await db.execute(
            select(Approval).where(
                Approval.status == ApprovalStatus.pending,
                Approval.expires_at < now,
            )
        )
        expired = result.scalars().all()

        for approval in expired:
            try:
                approval.status = ApprovalStatus.expired

                if escalation_target:
                    new_approval = Approval(
                        action_type=approval.action_type,
                        entity_type=approval.entity_type,
                        entity_id=approval.entity_id,
                        requested_by=approval.requested_by,
                        assigned_to=escalation_target.sub,
                        context=approval.context,
                    )
                    db.add(new_approval)

                escalated += 1
            except Exception as exc:
                logger.error("escalation_item_failed", approval_id=str(approval.id), error=str(exc))
                errors += 1

        await db.commit()

    logger.info("approval_escalation_done", escalated=escalated, errors=errors)
    return {"escalated": escalated, "errors": errors}
