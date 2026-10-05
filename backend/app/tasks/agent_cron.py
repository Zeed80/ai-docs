"""Durable intake for scheduled agent work.

Cron is a channel adapter, not a second in-memory agent runtime.  Each due
minute becomes one durable-chat intake identified by the schedule UUID and the
*scheduled* UTC minute.  A restart or two beat processes can therefore replay
the same occurrence safely, while tomorrow's identical schedule remains new.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# ── Minimal 5-field cron matcher ───────────────────────────────────────────────
# Supports: "*", "*/n", "a", "a-b", "a,b,c" (and combinations via commas).
# Deterministic and dependency-free; the dispatcher runs once a minute, so
# "due" means "the current minute matches and we did not already run in it".


def _field_matches(field: str, value: int, minimum: int = 0) -> bool:
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                continue
            if step > 0 and (value - minimum) % step == 0:
                return True
        elif "-" in part:
            try:
                lo, hi = (int(x) for x in part.split("-", 1))
            except ValueError:
                continue
            if lo <= value <= hi:
                return True
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue
    return False


def cron_matches(schedule: str, moment: datetime) -> bool:
    """True when the 5-field cron expression matches the given minute."""
    fields = (schedule or "").split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    return (
        _field_matches(minute, moment.minute)
        and _field_matches(hour, moment.hour)
        and _field_matches(dom, moment.day, minimum=1)
        and _field_matches(month, moment.month, minimum=1)
        # cron: 0=Sunday..6=Saturday; Python: Monday=0..Sunday=6.
        and _field_matches(dow, (moment.weekday() + 1) % 7)
    )


def _is_due(schedule: str, last_run_at: datetime | None, now: datetime) -> bool:
    if not cron_matches(schedule, now):
        return False
    if last_run_at is None:
        return True
    last = last_run_at if last_run_at.tzinfo else last_run_at.replace(tzinfo=UTC)
    # Already ran within the current minute → not due again.
    return last.replace(second=0, microsecond=0) < now.replace(second=0, microsecond=0)


def _due_minute(moment: datetime) -> datetime:
    """Canonical scheduled instant; never derive an occurrence ID from dispatch time."""
    moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).replace(second=0, microsecond=0)


def _occurrence_key(cron_id: uuid.UUID, due_at: datetime) -> str:
    return f"{cron_id}:{_due_minute(due_at).isoformat()}"


async def _intake_due_cron(*, cron_id: uuid.UUID, due_at: datetime, checked_at: datetime) -> bool:
    """Atomically persist a due occurrence, or reject it before any agent work.

    The row lock makes the old ``last_run_at`` field a useful fast path; the
    intake unique key is the durable replay invariant if the transaction is
    retried after a process restart.
    """
    from sqlalchemy import select

    from app.auth.models import UserRole
    from app.db.agent_runtime_models import DelegationGrant
    from app.db.models import AgentCron, AgentTask, User
    from app.db.session import _get_session_factory
    from app.domain.agent_intake import (
        AgentIntakeRequest,
        VerifiedIntakeIdentity,
        submit_agent_intake,
    )
    from app.domain.delegations import arguments_match

    factory = _get_session_factory()
    async with factory() as db:
        row = await db.get(AgentCron, cron_id, with_for_update=True)
        if row is None or not row.enabled or not _is_due(row.schedule, row.last_run_at, due_at):
            return False
        # Legacy rows and service identities never acquire an implicit owner.
        if not row.owner_key or row.owner_key in {"agent-service", "anonymous", "system:cron"}:
            logger.warning("agent_cron_blocked_unknown_owner id=%s", row.id)
            return False
        owner = await db.scalar(
            select(User).where(User.sub == row.owner_key, User.is_active.is_(True))
        )
        if owner is None:
            logger.warning(
                "agent_cron_blocked_inactive_owner id=%s owner=%s", row.id, row.owner_key
            )
            return False
        try:
            role = UserRole(owner.role)
        except ValueError:
            logger.warning("agent_cron_blocked_invalid_owner_role id=%s", row.id)
            return False
        if role is not UserRole.admin:
            logger.warning("agent_cron_blocked_owner_not_admin id=%s", row.id)
            return False
        if row.delegation_grant_id is None:
            logger.warning("agent_cron_blocked_missing_grant id=%s", row.id)
            return False
        grant = await db.scalar(
            select(DelegationGrant)
            .where(
                DelegationGrant.id == row.delegation_grant_id,
                DelegationGrant.owner_key == row.owner_key,
                DelegationGrant.revoked_at.is_(None),
                DelegationGrant.expires_at > checked_at,
                DelegationGrant.used_actions < DelegationGrant.max_actions,
            )
            .with_for_update()
        )
        grant_arguments = {
            "schedule": row.schedule,
            "prompt_sha256": hashlib.sha256(row.prompt.encode()).hexdigest(),
        }
        if (
            grant is None
            or grant.actions != ["agent.cron.run"]
            or not isinstance(grant.constraints, dict)
            or set(grant.constraints) != {"schedule", "prompt_sha256"}
            or not arguments_match(grant.constraints, grant_arguments)
        ):
            logger.warning("agent_cron_blocked_revoked_or_expired_grant id=%s", row.id)
            return False

        occurrence = _occurrence_key(row.id, due_at)
        result = await submit_agent_intake(
            db,
            identity=VerifiedIntakeIdentity(account_key=row.owner_key, channel="cron"),
            request=AgentIntakeRequest(
                channel="cron",
                external_message_id=occurrence,
                request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"cron:{occurrence}"),
                content=row.prompt,
                workspace_context={
                    "cron": {
                        "schedule_id": str(row.id),
                        "scheduled_for": _due_minute(due_at).isoformat(),
                    }
                },
            ),
            commit=False,
        )
        if not result.created:
            return False
        # Keep the legacy control-plane list auditable, but execution is owned
        # by the durable work order and its approval gates.
        legacy_task = AgentTask(
            objective=f"Cron: {(row.description or row.prompt)[:200]}",
            description=row.prompt,
            role="secretary",
            status="created",
            metadata_={
                "agent_cron_id": str(row.id),
                "schedule": row.schedule,
                "scheduled_for": _due_minute(due_at).isoformat(),
                "work_order_id": str(result.order.id),
            },
        )
        db.add(legacy_task)
        result.order.legacy_agent_task_id = legacy_task.id
        row.last_run_at = _due_minute(due_at)
        row.run_count += 1
        grant.used_actions += 1
        await db.commit()
        logger.info("agent_cron_intake_accepted id=%s work_order_id=%s", row.id, result.order.id)
        return True


async def _dispatch(now: datetime | None = None) -> int:
    """Intake current due schedules.

    Production calls this without an argument, so ``checked_at`` is always
    wall-clock UTC.  The optional moment exists solely as a deterministic test
    seam for cron matching and occurrence IDs.
    """
    from sqlalchemy import select

    from app.db.models import AgentCron
    from app.db.session import _get_session_factory

    checked_at = now or datetime.now(UTC)
    due_at = _due_minute(checked_at)
    factory = _get_session_factory()
    async with factory() as db:
        cron_ids = list(
            (await db.execute(select(AgentCron.id).where(AgentCron.enabled.is_(True)))).scalars()
        )
    accepted = 0
    for cron_id in cron_ids:
        if await _intake_due_cron(cron_id=cron_id, due_at=due_at, checked_at=checked_at):
            accepted += 1
    return accepted


@celery_app.task(
    name="agent.cron_dispatch",
    bind=True,
    max_retries=0,
    queue="scheduler",
    ignore_result=True,
)
def dispatch_agent_crons(self) -> None:  # type: ignore[override]
    """Persist due cron occurrences into the common durable intake."""
    try:
        run_async(_dispatch())
    except Exception as exc:
        logger.error("agent_cron_dispatch_failed", exc_info=exc)
