"""Public control API for durable autonomous work orders."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.auth.jwt import get_current_user, require_role
from app.auth.models import UserInfo, UserRole
from app.db.models import (
    Approval,
    ApprovalActionType,
    ComputerUseGrant,
    WorkAcceptanceCriterion,
    WorkArtifact,
    WorkEvent,
    WorkEvidence,
    WorkLearning,
    WorkOrder,
    WorkPlan,
    WorkStep,
    WorkStepAttempt,
    WorkToolCall,
)
from app.db.session import get_db
from app.domain.work_orders import (
    TERMINAL_WORK_STATUSES,
    WorkStateError,
    append_event,
    create_work_order,
    create_work_plan,
    record_verifier_verdict,
    transition_step,
    transition_work_order,
)

router = APIRouter()


class AcceptanceCriterionIn(BaseModel):
    criterion_key: str = Field(..., min_length=1, max_length=120)
    description: str = Field(..., min_length=1)
    kind: str = Field("semantic", min_length=1, max_length=40)
    required: bool = True
    predicate: dict[str, Any] = Field(default_factory=dict)


class WorkStepIn(BaseModel):
    step_key: str = Field(..., min_length=1, max_length=120)
    title: str = Field(..., min_length=1, max_length=500)
    kind: str = Field(..., pattern="^(agent_turn|capability)$")
    capability: str | None = Field(default=None, max_length=200)
    action: str | None = Field(default=None, max_length=200)
    input: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    success_predicate: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = Field("low", pattern="^(low|medium|high|critical)$")
    max_attempts: int = Field(3, ge=1, le=20)
    timeout_seconds: int = Field(600, ge=1, le=3600)
    retry_policy: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_executor(self) -> WorkStepIn:
        if self.kind == "capability" and (not self.capability or not self.action):
            raise ValueError("capability steps require capability and action")
        if self.step_key in self.depends_on:
            raise ValueError("a work step cannot depend on itself")
        return self


class WorkOrderCreate(BaseModel):
    objective: str = Field(..., min_length=1)
    description: str | None = None
    source: str = Field("api", min_length=1, max_length=50)
    priority: int = Field(50, ge=0, le=100)
    risk_level: str = Field("low", pattern="^(low|medium|high|critical)$")
    constraints: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[AcceptanceCriterionIn] = Field(default_factory=list)
    steps: list[WorkStepIn] = Field(default_factory=list)
    parent_id: uuid.UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_now: bool = False


class WorkInstructionIn(BaseModel):
    instruction: str = Field(..., min_length=1)
    # A client retry or double submit with the same id adds nothing (E23).
    request_id: str | None = Field(default=None, min_length=1, max_length=120)


class WorkApprovalIn(BaseModel):
    step_id: uuid.UUID
    capability: str = Field(..., min_length=1, max_length=200)
    action: str = Field(..., min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(..., min_length=1)
    assigned_to: str | None = None
    expires_at: datetime | None = None


class VerifierVerdictIn(BaseModel):
    ok: bool
    reason: str = Field(..., min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)
    # The plan revision whose result was judged; a mismatch is refused (E23).
    plan_revision: int | None = None


class ComputerUseGrantIn(BaseModel):
    actions: list[str] = Field(min_length=1, max_length=10)
    allowed_roots: list[str] = Field(default_factory=list, max_length=10)
    allowed_hosts: list[str] = Field(default_factory=list, max_length=50)
    allowed_commands: list[str] = Field(default_factory=list, max_length=30)
    max_actions: int = Field(20, ge=1, le=200)
    ttl_seconds: int = Field(3600, ge=60, le=86400)
    reason: str = Field(min_length=1, max_length=1000)
    # E34: stored browser secrets of the order's owner this grant may type.
    secret_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)


class WorkOrderOut(BaseModel):
    id: uuid.UUID
    owner_key: str
    source: str
    objective: str
    description: str | None
    status: str
    risk_level: str
    priority: int
    constraints: dict
    budgets: dict
    plan_revision: int
    result_summary: str | None
    blocker: dict | None
    deadline_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    parent_id: uuid.UUID | None
    metadata_: dict = Field(serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime
    # E45: computed by the server; the UI shows these, not its own guess.
    display_status: str = ""
    available_actions: list[str] = Field(default_factory=list)

    model_config = {"from_attributes": True, "populate_by_name": True}

    @model_validator(mode="after")
    def _owner_view(self) -> WorkOrderOut:
        from app.domain.work_orders import available_actions, display_status

        view = _OrderView(self)
        self.display_status = display_status(view)
        self.available_actions = available_actions(view)
        return self


class _OrderView:
    """Just the fields the transition rules read, from a WorkOrderOut."""

    def __init__(self, out: WorkOrderOut) -> None:
        self.status = out.status
        self.source = out.source
        self.metadata_ = out.metadata_


class WorkLearningOut(BaseModel):
    id: uuid.UUID
    work_order_id: uuid.UUID
    status: str
    summary: str | None
    lessons: list
    provenance: dict
    memory_fact_id: uuid.UUID | None
    recipe_skill_id: uuid.UUID | None
    extraction_attempts: int
    last_error: dict | None
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


def _is_admin(user: UserInfo) -> bool:
    return UserRole.admin in user.roles


async def _get_owned_order(
    db: AsyncSession, work_order_id: uuid.UUID, user: UserInfo, *, lock: bool = False
) -> WorkOrder:
    stmt = select(WorkOrder).where(WorkOrder.id == work_order_id)
    if not _is_admin(user):
        stmt = stmt.where(WorkOrder.owner_key == user.sub)
    if lock:
        stmt = stmt.with_for_update()
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    return order


@router.post("", response_model=WorkOrderOut, status_code=status.HTTP_201_CREATED)
async def create_order(
    body: WorkOrderCreate,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> WorkOrder:
    if body.parent_id is not None:
        # Resolve parent visibility before creating or committing a child.  The
        # shared-ledger initializer below remains authoritative for lineage and
        # same-owner binding.
        await _get_owned_order(db, body.parent_id, user, lock=True)
    criteria = [criterion.model_dump() for criterion in body.acceptance_criteria] or None
    if body.source == "durable_chat":
        raise HTTPException(409, "Durable chat requests must use /api/agent/chat-runs")
    order = await create_work_order(
        db,
        owner_key=user.sub,
        objective=body.objective,
        description=body.description,
        source=body.source,
        priority=body.priority,
        risk_level=body.risk_level,
        constraints=body.constraints,
        budgets=body.budgets,
        acceptance_criteria=criteria,
        parent_id=body.parent_id,
        metadata=body.metadata,
    )
    from app.domain.work_budget_ledger import BudgetLedgerError, initialize_budget_ledger

    try:
        await initialize_budget_ledger(db, order.id)
    except BudgetLedgerError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if body.steps:
        try:
            await create_work_plan(
                db,
                order,
                steps=[step.model_dump() for step in body.steps],
                actor=user.sub,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        from app.config import settings
        from app.domain.work_planning import plan_work_order

        if settings.app_env == "test":
            await plan_work_order(db, order, use_model=False)
        else:
            await transition_work_order(db, order, "planning", actor="capability-planner")
    await db.commit()
    await db.refresh(order)
    if not body.steps and body.run_now and order.status == "planning":
        from app.domain.work_planning import plan_work_order_detached

        await plan_work_order_detached(order.id)
        await db.refresh(order)
    elif not body.steps and not body.run_now and order.status == "planning":
        from app.tasks.work_orders import plan_work_order_task

        plan_work_order_task.apply_async(args=[str(order.id)], queue="scheduler")
    if body.run_now and order.status == "ready":
        from app.config import settings
        from app.tasks.work_orders import execute_work_order_now

        execution_factory = (
            async_sessionmaker(bind=db.bind, expire_on_commit=False)
            if settings.app_env == "test"
            else None
        )
        await execute_work_order_now(order.id, session_factory=execution_factory)
        await db.refresh(order)
    return order


@router.get("", response_model=list[WorkOrderOut])
async def list_orders(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> list[WorkOrder]:
    stmt = select(WorkOrder).order_by(WorkOrder.created_at.desc()).limit(limit)
    if not _is_admin(user):
        stmt = stmt.where(WorkOrder.owner_key == user.sub)
    if status_filter:
        stmt = stmt.where(WorkOrder.status == status_filter)
    return list((await db.execute(stmt)).scalars())


@router.get("/metrics")
async def get_work_order_metrics(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Б14: minimal operator-facing observability — counts by status and
    p50/p95 attempt duration by step kind/capability over the window.
    Deliberately not a full metrics/tracing stack (A6 found the per-turn
    trace already lives in structured logs) — this is the aggregate view an
    operator needs on the /work-orders page header, nothing more.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    status_query = (
        select(WorkOrder.status, func.count())
        .where(WorkOrder.created_at >= since)
        .group_by(WorkOrder.status)
    )
    if not _is_admin(user):
        status_query = status_query.where(WorkOrder.owner_key == user.sub)
    status_rows = (await db.execute(status_query)).all()

    duration_scope = (
        select(
            WorkStep.kind,
            WorkStep.capability,
            WorkStepAttempt.started_at,
            WorkStepAttempt.finished_at,
        )
        .join(WorkStep, WorkStep.id == WorkStepAttempt.step_id)
        .join(WorkOrder, WorkOrder.id == WorkStep.work_order_id)
        .where(
            WorkStepAttempt.status == "succeeded",
            WorkStepAttempt.finished_at.isnot(None),
            WorkStepAttempt.finished_at >= since,
        )
    )
    if not _is_admin(user):
        duration_scope = duration_scope.where(WorkOrder.owner_key == user.sub)
    duration_rows = (await db.execute(duration_scope)).all()

    # Percentiles computed in Python, not a Postgres-only percentile_cont —
    # same SQLite/Postgres portability reasoning as enforce_budgets (A4/Б15).
    by_group: dict[str, list[float]] = {}
    for kind, capability, started_at, finished_at in duration_rows:
        if started_at is None:
            continue
        key = f"{capability}.{kind}" if capability else kind
        by_group.setdefault(key, []).append((finished_at - started_at).total_seconds())

    def _percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
        return round(ordered[idx], 2)

    durations = {
        key: {
            "count": len(values),
            "p50_seconds": _percentile(values, 0.5),
            "p95_seconds": _percentile(values, 0.95),
        }
        for key, values in by_group.items()
    }

    return {
        "window_hours": hours,
        "status_counts": dict(status_rows),
        "step_durations": durations,
    }


@router.get("/{work_order_id}", response_model=WorkOrderOut)
async def get_order(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> WorkOrder:
    return await _get_owned_order(db, work_order_id, user)


@router.get("/{work_order_id}/budget")
async def get_work_order_budget(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """E45: the shared budget of this order's line — limits and what is used.

    "used" counts charged units and still-open reservations, the same sum
    the ledger checks before every new reservation; a limit of null is no
    limit, and a dimension never measured reads as unknown, not zero.
    """
    from app.db.work_budget_models import WorkBudgetLedger
    from app.domain.work_budget_ledger import DIMENSION_LIMITS, _usage

    order = await _get_owned_order(db, work_order_id, user)
    ledger_id = getattr(order, "budget_ledger_id", None)
    ledger = await db.get(WorkBudgetLedger, ledger_id) if ledger_id else None
    if ledger is None:
        return {"work_order_id": str(order.id), "ledger": None, "dimensions": {}}
    dimensions = {}
    for dimension, limit_field in DIMENSION_LIMITS.items():
        limit = getattr(ledger, limit_field)
        used = await _usage(db, ledger.id, dimension)
        dimensions[dimension] = {
            "limit": None if limit is None else float(limit),
            "used": float(used),
        }
    return {
        "work_order_id": str(order.id),
        "ledger": str(ledger.id),
        "root_work_order_id": str(ledger.root_work_order_id),
        "blocker": ledger.blocker,
        "dimensions": dimensions,
    }


@router.get("/{work_order_id}/learning", response_model=WorkLearningOut)
async def get_work_learning(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> WorkLearning:
    order = await _get_owned_order(db, work_order_id, user)
    learning = (
        await db.execute(select(WorkLearning).where(WorkLearning.work_order_id == order.id))
    ).scalar_one_or_none()
    if learning is None:
        raise HTTPException(status_code=404, detail="Work learning not found")
    return learning


@router.post("/{work_order_id}/learning/reprocess", response_model=WorkLearningOut)
async def reprocess_work_learning(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> WorkLearning:
    order = await _get_owned_order(db, work_order_id, user)
    if order.status != "completed":
        raise HTTPException(status_code=409, detail="Only completed work can be learned")
    from app.domain.work_learning import reset_work_learning

    learning = await reset_work_learning(db, order.id)
    if learning is None:
        learning = WorkLearning(work_order_id=order.id, status="pending")
        db.add(learning)
        await db.flush()
    await db.commit()
    await db.refresh(learning)
    from app.tasks.work_orders import learn_work_order

    learn_work_order.apply_async(args=[str(order.id)], queue="scheduler")
    return learning


@router.get("/{work_order_id}/plan")
async def get_plan(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    order = await _get_owned_order(db, work_order_id, user)
    plan = (
        await db.execute(
            select(WorkPlan)
            .where(WorkPlan.work_order_id == order.id)
            .options(selectinload(WorkPlan.steps))
            .order_by(WorkPlan.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="Work plan not found")
    return {
        "id": str(plan.id),
        "revision": plan.revision,
        "status": plan.status,
        "goal": plan.goal,
        "assumptions": plan.assumptions,
        "verification_plan": plan.verification_plan,
        "steps": [
            {
                "id": str(step.id),
                "step_key": step.step_key,
                "title": step.title,
                "kind": step.kind,
                "capability": step.capability,
                "action": step.action,
                "depends_on": step.depends_on,
                "state": step.state,
                "attempt_count": step.attempt_count,
                "max_attempts": step.max_attempts,
                "output": step.output,
                "last_error": step.last_error,
            }
            for step in sorted(plan.steps, key=lambda item: item.created_at)
        ],
    }


@router.get("/{work_order_id}/criteria")
async def get_criteria(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> list[dict]:
    order = await _get_owned_order(db, work_order_id, user)
    rows = list(
        (
            await db.execute(
                select(WorkAcceptanceCriterion)
                .where(WorkAcceptanceCriterion.work_order_id == order.id)
                .order_by(WorkAcceptanceCriterion.created_at)
            )
        ).scalars()
    )
    return [
        {
            "id": str(row.id),
            "criterion_key": row.criterion_key,
            "description": row.description,
            "kind": row.kind,
            "required": row.required,
            "predicate": row.predicate,
            "status": row.status,
            "verdict": row.verdict,
            "verified_at": row.verified_at,
            "verified_by": row.verified_by,
        }
        for row in rows
    ]


@router.get("/{work_order_id}/events")
async def get_events(
    work_order_id: uuid.UUID,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> list[dict]:
    order = await _get_owned_order(db, work_order_id, user)
    rows = list(
        (
            await db.execute(
                select(WorkEvent)
                .where(WorkEvent.work_order_id == order.id, WorkEvent.sequence > after)
                .order_by(WorkEvent.sequence)
                .limit(limit)
            )
        ).scalars()
    )
    return [
        {
            "id": str(row.id),
            "sequence": row.sequence,
            "event_type": row.event_type,
            "actor": row.actor,
            "payload": row.payload,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/{work_order_id}/criteria/{criterion_id}/verdict")
async def submit_verifier_verdict(
    work_order_id: uuid.UUID,
    criterion_id: uuid.UUID,
    body: VerifierVerdictIn,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(require_role(UserRole.manager)),
) -> dict:
    """Independent verifier/human-manager acceptance decision."""
    order = await db.get(WorkOrder, work_order_id, with_for_update=True)
    criterion = await db.get(WorkAcceptanceCriterion, criterion_id, with_for_update=True)
    if order is None or criterion is None or criterion.work_order_id != order.id:
        raise HTTPException(status_code=404, detail="Work order criterion not found")
    try:
        completed = await record_verifier_verdict(
            db,
            order=order,
            criterion=criterion,
            ok=body.ok,
            reason=body.reason,
            evidence_payload=body.evidence,
            actor=user.sub,
            expected_revision=body.plan_revision,
        )
    except WorkStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return {
        "work_order_id": str(order.id),
        "criterion_id": str(criterion.id),
        "criterion_status": criterion.status,
        "work_order_status": order.status,
        "completed": completed,
    }


@router.get("/{work_order_id}/evidence")
async def get_evidence(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    order = await _get_owned_order(db, work_order_id, user)
    evidence_result = await db.execute(
        select(WorkEvidence).where(WorkEvidence.work_order_id == order.id)
    )
    evidence = list(evidence_result.scalars())
    artifact_result = await db.execute(
        select(WorkArtifact).where(WorkArtifact.work_order_id == order.id)
    )
    artifacts = list(artifact_result.scalars())
    return {
        "evidence": [
            {
                "id": str(row.id),
                "criterion_id": str(row.criterion_id) if row.criterion_id else None,
                "step_id": str(row.step_id) if row.step_id else None,
                "evidence_type": row.evidence_type,
                "source": row.source,
                "payload": row.payload,
                "content_hash": row.content_hash,
                "verifier_status": row.verifier_status,
            }
            for row in evidence
        ],
        "artifacts": [
            {
                "id": str(row.id),
                "step_id": str(row.step_id) if row.step_id else None,
                "artifact_type": row.artifact_type,
                "name": row.name,
                "uri": row.uri,
                "content_hash": row.content_hash,
                "content_type": row.content_type,
                "size_bytes": row.size_bytes,
                "metadata": row.metadata_,
            }
            for row in artifacts
        ],
    }


@router.post("/{work_order_id}/computer-grants", status_code=status.HTTP_201_CREATED)
async def grant_computer_use(
    work_order_id: uuid.UUID,
    body: ComputerUseGrantIn,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(require_role(UserRole.manager)),
) -> dict:
    order = await db.get(WorkOrder, work_order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    valid_actions = {
        "browser_fetch",
        "desktop_snapshot",
        "desktop_start",
        "desktop_observe",
        "desktop_prepare_submit",
        "desktop_click",
        "desktop_type",
        "desktop_read",
        "desktop_navigate",
        "desktop_tabs",
        "desktop_fill_secret",
        "desktop_upload",
        "desktop_downloads",
        "desktop_save_download",
        "desktop_close",
        "file_read",
        "file_write",
        "shell",
    }
    if not set(body.actions) <= valid_actions:
        raise HTTPException(status_code=422, detail="Unknown computer-use action")
    if any(root in {"/", "~", ""} or not root.startswith("/") for root in body.allowed_roots):
        raise HTTPException(status_code=422, detail="Allowed roots must be narrow absolute paths")
    if body.secret_ids:
        from app.db.models import BrowserSecret

        if "desktop_fill_secret" not in body.actions:
            raise HTTPException(422, "secret_ids need the desktop_fill_secret action")
        found = list(
            await db.scalars(select(BrowserSecret).where(BrowserSecret.id.in_(body.secret_ids)))
        )
        # Only the order owner's own, live secrets: a manager granting the
        # work cannot hand it someone else's password.
        if len(found) != len(set(body.secret_ids)) or any(
            s.owner_sub != order.owner_key or s.revoked_at is not None for s in found
        ):
            raise HTTPException(422, "Unknown, foreign or revoked browser secret")
    grant = ComputerUseGrant(
        work_order_id=order.id,
        granted_to=order.owner_key,
        secret_ids=[str(i) for i in dict.fromkeys(body.secret_ids)],
        granted_by=user.sub,
        actions=list(dict.fromkeys(body.actions)),
        allowed_roots=list(dict.fromkeys(body.allowed_roots)),
        allowed_hosts=list(dict.fromkeys(body.allowed_hosts)),
        allowed_commands=list(dict.fromkeys(body.allowed_commands)),
        max_actions=body.max_actions,
        expires_at=datetime.now(UTC) + timedelta(seconds=body.ttl_seconds),
        reason=body.reason,
    )
    db.add(grant)
    await db.flush()
    await append_event(
        db,
        order.id,
        "computer_use.granted",
        actor=user.sub,
        payload={
            "grant_id": str(grant.id),
            "actions": grant.actions,
            "expires_at": grant.expires_at.isoformat(),
            "secret_ids": grant.secret_ids,
        },
    )
    await db.commit()
    return {
        "id": str(grant.id),
        "granted_to": grant.granted_to,
        "actions": grant.actions,
        "expires_at": grant.expires_at,
    }


@router.post("/{work_order_id}/artifacts/{artifact_id}/revoke")
async def revoke_work_artifact(
    work_order_id: uuid.UUID,
    artifact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """E35: an artifact the owner withdrew is no longer an input anywhere.

    The bytes stay (content-addressed, never overwritten); the broker refuses
    a revoked artifact as an upload or a script input from now on.
    """
    from app.db.models import WorkArtifact

    order = await db.get(WorkOrder, work_order_id)
    artifact = await db.get(WorkArtifact, artifact_id, with_for_update=True)
    if (
        order is None
        or artifact is None
        or artifact.work_order_id != order.id
        or order.owner_key != user.sub
    ):
        raise HTTPException(status_code=404, detail="Artifact not found")
    meta = dict(artifact.metadata_ or {})
    if not meta.get("revoked_at"):
        meta["revoked_at"] = datetime.now(UTC).isoformat()
        meta["revoked_by"] = user.sub
        artifact.metadata_ = meta
        await append_event(
            db,
            order.id,
            "artifact.revoked",
            actor=user.sub,
            payload={"artifact_id": str(artifact.id), "sha256": artifact.content_hash},
        )
        await db.commit()
    return {"id": str(artifact.id), "revoked_at": meta["revoked_at"]}


@router.post("/{work_order_id}/computer-grants/{grant_id}/revoke")
async def revoke_computer_use(
    work_order_id: uuid.UUID,
    grant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(require_role(UserRole.manager)),
) -> dict:
    """E24: take a computer-use grant back before it expires.

    There was no way to: a grant lived until its TTL or action limit. The
    executor locks the grant and checks revoked_at in the same commit that
    consumes it, so a revoked grant authorizes nothing more; an action that
    already passed that commit is not undone.
    """
    grant = await db.get(ComputerUseGrant, grant_id, with_for_update=True)
    if grant is None or grant.work_order_id != work_order_id:
        raise HTTPException(status_code=404, detail="Computer-use grant not found")
    if grant.revoked_at is None:
        grant.revoked_at = datetime.now(UTC)
        await append_event(
            db,
            work_order_id,
            "computer_use.revoked",
            actor=user.sub,
            payload={"grant_id": str(grant.id), "used_actions": grant.used_actions},
        )
        await db.commit()
        # E31: an open browser session must not outlive the right to use it.
        from app.api.computer_use import close_browser_sessions

        await close_browser_sessions(work_order_id)
    return {"id": str(grant.id), "revoked_at": grant.revoked_at}


@router.get("/{work_order_id}/tool-calls")
async def get_tool_calls(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> list[dict]:
    order = await _get_owned_order(db, work_order_id, user)
    rows = list(
        (
            await db.execute(
                select(WorkToolCall)
                .where(WorkToolCall.work_order_id == order.id)
                .order_by(WorkToolCall.created_at)
            )
        ).scalars()
    )
    return [
        {
            "id": str(row.id),
            "step_id": str(row.step_id),
            "attempt_id": str(row.attempt_id),
            "executor": row.executor,
            "capability": row.capability,
            "action": row.action,
            "arguments": row.arguments,
            "resolved_from": row.resolved_from,
            "risk_level": row.risk_level,
            "status": row.status,
            "action_digest": row.action_digest,
            "output": row.output,
            "error": row.error,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
        }
        for row in rows
    ]


@router.post("/{work_order_id}/instructions", response_model=WorkOrderOut)
async def add_instruction(
    work_order_id: uuid.UUID,
    body: WorkInstructionIn,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> WorkOrder:
    order = await _get_owned_order(db, work_order_id, user, lock=True)
    if order.source == "durable_chat":
        raise HTTPException(409, "Durable chat checkpoint continuation is not enabled")
    if order.status in {"completed", "canceled"}:
        raise HTTPException(status_code=409, detail=f"Cannot revise a {order.status} work order")
    metadata = dict(order.metadata_ or {})
    instructions = list(metadata.get("instructions") or [])
    if body.request_id and any(
        isinstance(item, dict) and item.get("request_id") == body.request_id
        for item in instructions
    ):
        return order
    from app.domain.work_orders import unresolved_effect_call_ids

    unresolved = await unresolved_effect_call_ids(db, order.id)
    if unresolved:
        # E23: replanning now could repeat an effect whose outcome is unknown.
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "unresolved_effect_requires_reconciliation",
                "message": "Сначала сверьте действия с неизвестным исходом",
                "tool_call_ids": unresolved,
            },
        )
    entry = {"text": body.instruction, "actor": user.sub, "at": datetime.now(UTC).isoformat()}
    if body.request_id:
        entry["request_id"] = body.request_id
    instructions.append(entry)
    metadata["instructions"] = instructions
    order.metadata_ = metadata
    await append_event(
        db,
        order.id,
        "work.instruction_added",
        actor=user.sub,
        payload={"instruction": body.instruction},
    )
    if order.status in {"blocked", "failed"}:
        await transition_work_order(db, order, "planning", actor=user.sub)
    elif order.status not in TERMINAL_WORK_STATUSES and order.status != "replanning":
        await transition_work_order(db, order, "replanning", actor=user.sub)
    await db.commit()
    await db.refresh(order)
    return order


@router.post("/{work_order_id}/approvals", status_code=status.HTTP_201_CREATED)
async def request_work_approval(
    work_order_id: uuid.UUID,
    body: WorkApprovalIn,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Persist a least-privilege approval bound to exact step arguments."""
    order = await _get_owned_order(db, work_order_id, user, lock=True)
    step = await db.get(WorkStep, body.step_id, with_for_update=True)
    if step is None or step.work_order_id != order.id:
        raise HTTPException(status_code=404, detail="Work step not found")
    if order.status not in {"ready", "running"} or step.state not in {"ready", "running"}:
        raise HTTPException(status_code=409, detail="Work step is not approval-gateable")
    action_payload = {
        "capability": body.capability,
        "action": body.action,
        "arguments": body.arguments,
    }
    action_digest = hashlib.sha256(
        json.dumps(action_payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    approval = Approval(
        action_type=ApprovalActionType.agent_tool_call,
        entity_type="work_order",
        entity_id=order.id,
        requested_by=user.sub,
        assigned_to=body.assigned_to,
        expires_at=body.expires_at,
        context={
            "work_order_id": str(order.id),
            "step_id": str(step.id),
            "tool_name": body.capability,
            "action": body.action,
            "tool_args": body.arguments,
            "reason": body.reason,
            "action_digest": action_digest,
        },
    )
    db.add(approval)
    await db.flush()
    await transition_step(
        db,
        step,
        "waiting_approval",
        actor=user.sub,
        payload={"approval_id": str(approval.id), "action_digest": action_digest},
    )
    await transition_work_order(
        db,
        order,
        "waiting_approval",
        actor=user.sub,
        payload={"approval_id": str(approval.id), "step_id": str(step.id)},
    )
    # A Telegram approval button is a presentation of this exact persisted
    # decision, never an alternative decision endpoint. Only an explicitly
    # assigned active manager/admin with a private verified binding receives a
    # callback and its owner-bound outbox reference.
    if approval.assigned_to and approval.expires_at is not None:
        from app.db.agent_runtime_models import AgentChannelIdentity
        from app.db.models import User
        from app.domain.agent_outbox import AgentOutboxRequest, produce_agent_outbox
        from app.domain.telegram_approvals import (
            TelegramApprovalError,
            create_telegram_approval_callback,
        )

        bindings = (
            await db.scalars(
                select(AgentChannelIdentity)
                .join(User, User.sub == AgentChannelIdentity.owner_key)
                .where(
                    AgentChannelIdentity.channel == "telegram",
                    AgentChannelIdentity.owner_key == approval.assigned_to,
                    AgentChannelIdentity.is_active.is_(True),
                    User.is_active.is_(True),
                    User.role.in_(["manager", "admin"]),
                )
                .with_for_update()
            )
        ).all()
        for binding in bindings:
            try:
                await create_telegram_approval_callback(db, approval=approval, binding=binding)
            except TelegramApprovalError:
                continue
            await produce_agent_outbox(
                db,
                request=AgentOutboxRequest(
                    work_order_id=order.id,
                    owner_key=binding.owner_key,
                    destination_binding_id=binding.id,
                    event_type="approval.requested",
                    payload={"resource_type": "work_order", "resource_id": str(order.id)},
                    dedup_key=f"telegram:approval:{approval.id}:requested",
                    actor="work-order-approval",
                    approval_id=approval.id,
                ),
            )
    await db.commit()
    return {
        "approval_id": str(approval.id),
        "work_order_id": str(order.id),
        "step_id": str(step.id),
        "status": approval.status.value,
        "action_digest": action_digest,
    }


@router.post("/{work_order_id}/cancel", response_model=WorkOrderOut)
async def cancel_order(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> WorkOrder:
    order = await _get_owned_order(db, work_order_id, user, lock=True)
    if order.status in TERMINAL_WORK_STATUSES:
        if order.status == "canceled":
            return order
        raise HTTPException(status_code=409, detail=f"Cannot cancel a {order.status} work order")
    from app.domain.script_runs import kill_script_runs, mark_script_runs_canceled
    from app.domain.work_orders import cancel_work_order

    try:
        await cancel_work_order(db, order, actor=user.sub)
    except WorkStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # E29: the order's script runs are canceled in the same commit and their
    # containers killed after it; nothing here needs a second session.
    script_runs = await mark_script_runs_canceled(db, order.id)
    await db.commit()
    await db.refresh(order)
    if script_runs:
        await kill_script_runs(script_runs, order.owner_key)
    from app.api.computer_use import close_browser_sessions

    await close_browser_sessions(order.id)
    return order


class ToolCallReconcileIn(BaseModel):
    outcome: Literal["happened", "not_happened"]
    note: str = Field("", max_length=2000)


@router.post("/{work_order_id}/tool-calls/{tool_call_id}/reconcile")
async def reconcile_tool_call(
    work_order_id: uuid.UUID,
    tool_call_id: uuid.UUID,
    body: ToolCallReconcileIn,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict[str, Any]:
    """E23: the owner records whether an unknown-outcome effect happened.

    This is the owner's statement, kept as such: it does not verify the
    recipient. "happened" makes the call completed work for the planner, so a
    replan does not repeat it; "not_happened" lets a replan try it again.
    """
    from app.domain.work_orders import UNRESOLVED_EFFECT_STATUSES

    order = await _get_owned_order(db, work_order_id, user, lock=True)
    call = await db.get(WorkToolCall, tool_call_id, with_for_update=True)
    if call is None or call.work_order_id != order.id:
        raise HTTPException(status_code=404, detail="Tool call not found")
    if call.status not in UNRESOLVED_EFFECT_STATUSES:
        raise HTTPException(status_code=409, detail=f"Tool call is not unresolved: {call.status}")
    previous = call.status
    call.status = f"reconciled_{body.outcome}"
    call.error = {
        **(call.error or {}),
        "reconciliation": {
            "outcome": body.outcome,
            "previous_status": previous,
            "by": user.sub,
            "at": datetime.now(UTC).isoformat(),
            "note": body.note,
            "basis": "owner_statement",
        },
    }
    await append_event(
        db,
        order.id,
        "tool_call.reconciled",
        actor=user.sub,
        payload={"tool_call_id": str(call.id), "outcome": body.outcome, "previous": previous},
    )
    await db.commit()
    return {"tool_call_id": str(call.id), "status": call.status}


@router.post("/{work_order_id}/pause", response_model=WorkOrderOut)
async def pause_order(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> WorkOrder:
    """E22: stop claiming new steps; the order is paused once nothing executes.

    The response status is "paused" when acknowledged at once; otherwise the
    order keeps its status with ``metadata.pause`` set until the executing
    step finishes. A started step is finished, never treated as undone.
    """
    from app.domain.work_orders import request_pause

    order = await _get_owned_order(db, work_order_id, user, lock=True)
    try:
        await request_pause(db, order, actor=user.sub)
    except WorkStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(order)
    return order


@router.post("/{work_order_id}/unpause", response_model=WorkOrderOut)
async def unpause_order(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> WorkOrder:
    """E22: resume a paused order (or withdraw a pending pause request).

    Ownership is checked now, not at pause time; the shared budget is not reset.
    """
    from app.domain.work_orders import resume_paused

    order = await _get_owned_order(db, work_order_id, user, lock=True)
    try:
        await resume_paused(db, order, actor=user.sub)
    except WorkStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(order)
    return order


@router.post("/{work_order_id}/run", response_model=WorkOrderOut)
async def run_order(
    work_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> WorkOrder:
    order = await _get_owned_order(db, work_order_id, user)
    if order.source == "durable_chat":
        raise HTTPException(409, "Durable chat execution belongs to the background worker")
    if order.status not in {"ready", "running"}:
        raise HTTPException(status_code=409, detail=f"Work order is not runnable: {order.status}")
    from app.config import settings
    from app.tasks.work_orders import execute_work_order_now

    execution_factory = (
        async_sessionmaker(bind=db.bind, expire_on_commit=False)
        if settings.app_env == "test"
        else None
    )
    claimed = await execute_work_order_now(order.id, session_factory=execution_factory)
    if not claimed:
        raise HTTPException(status_code=409, detail="No ready work step could be claimed")
    await db.refresh(order)
    return order
