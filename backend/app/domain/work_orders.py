"""Durable autonomous work-order state machine and persistence services."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    WorkAcceptanceCriterion,
    WorkEvent,
    WorkEvidence,
    WorkLearning,
    WorkOrder,
    WorkPlan,
    WorkStep,
    WorkStepAttempt,
)

TERMINAL_WORK_STATUSES = frozenset({"completed", "blocked", "failed", "canceled"})
ACTIVE_WORK_STATUSES = frozenset(
    {
        "received",
        "scoping",
        "planning",
        "ready",
        "running",
        "waiting_approval",
        "waiting_external",
        "verifying",
        "replanning",
    }
)

WORK_TRANSITIONS: dict[str, frozenset[str]] = {
    "received": frozenset({"scoping", "planning", "canceled", "blocked"}),
    "scoping": frozenset({"planning", "blocked", "canceled"}),
    "planning": frozenset({"ready", "blocked", "failed", "canceled"}),
    "ready": frozenset(
        {"running", "waiting_approval", "replanning", "blocked", "canceled"}
    ),
    "running": frozenset(
        {
            "ready",
            "waiting_approval",
            "waiting_external",
            "verifying",
            "replanning",
            "blocked",
            "failed",
            "canceled",
        }
    ),
    "waiting_approval": frozenset({"ready", "replanning", "blocked", "canceled"}),
    "waiting_external": frozenset({"ready", "replanning", "blocked", "canceled"}),
    "verifying": frozenset({"completed", "replanning", "blocked", "failed", "canceled"}),
    "replanning": frozenset({"ready", "blocked", "failed", "canceled"}),
    "blocked": frozenset({"scoping", "planning", "ready", "verifying", "canceled"}),
    "failed": frozenset({"planning", "ready", "canceled"}),
    "completed": frozenset(),
    "canceled": frozenset(),
}

STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"ready", "skipped", "canceled"}),
    "ready": frozenset({"running", "waiting_approval", "canceled"}),
    "running": frozenset(
        {"succeeded", "retry_wait", "waiting_approval", "failed", "canceled"}
    ),
    "retry_wait": frozenset({"ready", "failed", "canceled"}),
    "waiting_approval": frozenset({"ready", "failed", "canceled"}),
    "succeeded": frozenset(),
    "failed": frozenset({"ready", "canceled"}),
    "skipped": frozenset(),
    "canceled": frozenset(),
}


class WorkStateError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_idempotency_key(work_order_id: uuid.UUID, revision: int, step_key: str) -> str:
    raw = f"{work_order_id}:{revision}:{step_key}".encode()
    return f"work:{hashlib.sha256(raw).hexdigest()}"


async def append_event(
    db: AsyncSession,
    work_order_id: uuid.UUID,
    event_type: str,
    *,
    actor: str,
    payload: dict[str, Any] | None = None,
) -> WorkEvent:
    # Locking the parent serializes sequence allocation for one work order.
    await db.execute(select(WorkOrder.id).where(WorkOrder.id == work_order_id).with_for_update())
    sequence = (
        int(
            (
                await db.execute(
                    select(func.coalesce(func.max(WorkEvent.sequence), 0)).where(
                        WorkEvent.work_order_id == work_order_id
                    )
                )
            ).scalar_one()
        )
        + 1
    )
    event = WorkEvent(
        work_order_id=work_order_id,
        sequence=sequence,
        event_type=event_type,
        actor=actor,
        payload=payload or {},
    )
    db.add(event)
    await db.flush()
    return event


async def transition_work_order(
    db: AsyncSession,
    work_order: WorkOrder,
    target: str,
    *,
    actor: str,
    payload: dict[str, Any] | None = None,
) -> WorkOrder:
    current = str(work_order.status)
    if target == current:
        return work_order
    if target not in WORK_TRANSITIONS.get(current, frozenset()):
        raise WorkStateError(f"Invalid work-order transition: {current} -> {target}")
    if target == "completed":
        await assert_completion_allowed(db, work_order.id)
        work_order.completed_at = utcnow()
        learning_exists = (
            await db.execute(
                select(WorkLearning.id).where(WorkLearning.work_order_id == work_order.id)
            )
        ).scalar_one_or_none()
        if learning_exists is None:
            db.add(WorkLearning(work_order_id=work_order.id, status="pending"))
    elif target == "canceled":
        work_order.canceled_at = utcnow()
    elif target == "running" and work_order.started_at is None:
        work_order.started_at = utcnow()
    work_order.status = target
    work_order.version += 1
    work_order.lease_owner = None
    work_order.lease_expires_at = None
    await append_event(
        db,
        work_order.id,
        "work.status_changed",
        actor=actor,
        payload={"from": current, "to": target, **(payload or {})},
    )
    await db.flush()
    return work_order


async def transition_step(
    db: AsyncSession,
    step: WorkStep,
    target: str,
    *,
    actor: str,
    payload: dict[str, Any] | None = None,
) -> WorkStep:
    current = str(step.state)
    if target == current:
        return step
    if target not in STEP_TRANSITIONS.get(current, frozenset()):
        raise WorkStateError(f"Invalid work-step transition: {current} -> {target}")
    step.state = target
    if target == "running" and step.started_at is None:
        step.started_at = utcnow()
    if target in {"succeeded", "failed", "skipped", "canceled"}:
        step.finished_at = utcnow()
        step.lease_owner = None
        step.lease_expires_at = None
    await append_event(
        db,
        step.work_order_id,
        "step.status_changed",
        actor=actor,
        payload={
            "step_id": str(step.id),
            "step_key": step.step_key,
            "from": current,
            "to": target,
            **(payload or {}),
        },
    )
    await db.flush()
    return step


async def create_work_order(
    db: AsyncSession,
    *,
    owner_key: str,
    objective: str,
    description: str | None = None,
    source: str = "api",
    priority: int = 50,
    risk_level: str = "low",
    constraints: dict[str, Any] | None = None,
    budgets: dict[str, Any] | None = None,
    acceptance_criteria: list[dict[str, Any]] | None = None,
    parent_id: uuid.UUID | None = None,
    legacy_agent_task_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> WorkOrder:
    if not objective.strip():
        raise ValueError("objective must not be empty")
    order = WorkOrder(
        owner_key=owner_key,
        source=source,
        objective=objective.strip(),
        description=(description or "").strip() or None,
        priority=max(0, min(int(priority), 100)),
        risk_level=risk_level,
        constraints=constraints or {},
        budgets=budgets or {},
        policy_snapshot={},
        parent_id=parent_id,
        legacy_agent_task_id=legacy_agent_task_id,
        metadata_=metadata or {},
    )
    db.add(order)
    await db.flush()
    criteria = acceptance_criteria or [
        {
            "criterion_key": "result_present",
            "description": "Исполнитель создал непустой проверяемый результат.",
            "kind": "artifact",
            "predicate": {"type": "nonempty_result"},
            "required": True,
        }
    ]
    for item in criteria:
        db.add(
            WorkAcceptanceCriterion(
                work_order_id=order.id,
                criterion_key=str(item["criterion_key"]),
                description=str(item["description"]),
                kind=str(item.get("kind") or "semantic"),
                predicate=dict(item.get("predicate") or {}),
                required=bool(item.get("required", True)),
            )
        )
    await append_event(
        db,
        order.id,
        "work.created",
        actor=owner_key,
        payload={"source": source, "risk_level": risk_level},
    )
    await db.flush()
    return order


async def create_single_step_plan(
    db: AsyncSession,
    work_order: WorkOrder,
    *,
    kind: str,
    title: str,
    input_data: dict[str, Any],
    capability: str | None = None,
    action: str | None = None,
    success_predicate: dict[str, Any] | None = None,
    max_attempts: int = 3,
    timeout_seconds: int = 600,
    actor: str = "planner",
) -> tuple[WorkPlan, WorkStep]:
    plan, steps = await create_work_plan(
        db,
        work_order,
        steps=[
            {
                "step_key": "execute",
                "title": title,
                "kind": kind,
                "capability": capability,
                "action": action,
                "input": input_data,
                "depends_on": [],
                "success_predicate": success_predicate
                or {"type": "no_error_and_nonempty_output"},
                "max_attempts": max_attempts,
                "timeout_seconds": timeout_seconds,
            }
        ],
        actor=actor,
    )
    return plan, steps[0]


def _validate_plan_steps(steps: list[dict[str, Any]]) -> None:
    if not steps:
        raise ValueError("work plan requires at least one step")
    keys = [str(item.get("step_key") or "").strip() for item in steps]
    if any(not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("work plan step keys must be non-empty and unique")
    key_set = set(keys)
    dependencies = {
        key: [str(value) for value in (item.get("depends_on") or [])]
        for key, item in zip(keys, steps, strict=True)
    }
    if any(dep not in key_set for values in dependencies.values() for dep in values):
        raise ValueError("work plan contains a dependency on an unknown step")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError("work plan dependencies contain a cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in dependencies[key]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in keys:
        visit(key)


async def create_work_plan(
    db: AsyncSession,
    work_order: WorkOrder,
    *,
    steps: list[dict[str, Any]],
    assumptions: list[Any] | None = None,
    verification_plan: dict[str, Any] | None = None,
    actor: str = "planner",
) -> tuple[WorkPlan, list[WorkStep]]:
    """Persist a validated DAG plan and make its root steps dispatchable."""
    _validate_plan_steps(steps)
    if work_order.status not in {"planning", "replanning"}:
        await transition_work_order(db, work_order, "planning", actor=actor)
    revision = work_order.plan_revision + 1
    existing_plans = list(
        (
            await db.execute(
                select(WorkPlan).where(
                    WorkPlan.work_order_id == work_order.id,
                    WorkPlan.status == "active",
                )
            )
        ).scalars()
    )
    for existing in existing_plans:
        existing.status = "superseded"
    plan = WorkPlan(
        work_order_id=work_order.id,
        revision=revision,
        goal=work_order.objective,
        assumptions=assumptions or [],
        verification_plan=verification_plan
        or {"mode": "deterministic_then_independent"},
        created_by=actor,
    )
    db.add(plan)
    await db.flush()
    rows: list[WorkStep] = []
    for item in steps:
        step_key = str(item["step_key"])
        depends_on = [str(value) for value in (item.get("depends_on") or [])]
        row = WorkStep(
            work_order_id=work_order.id,
            plan_id=plan.id,
            step_key=step_key,
            title=str(item.get("title") or step_key),
            kind=str(item.get("kind") or "agent_turn"),
            capability=item.get("capability"),
            action=item.get("action"),
            input_=dict(item.get("input") or {}),
            depends_on=depends_on,
            success_predicate=dict(
                item.get("success_predicate")
                or {"type": "no_error_and_nonempty_output"}
            ),
            state="pending" if depends_on else "ready",
            risk_level=str(item.get("risk_level") or "low"),
            max_attempts=max(1, int(item.get("max_attempts", 3))),
            timeout_seconds=max(1, int(item.get("timeout_seconds", 600))),
            retry_policy=dict(
                item.get("retry_policy")
                or {"strategy": "exponential", "base_seconds": 5, "max_seconds": 300}
            ),
            idempotency_key=make_idempotency_key(work_order.id, revision, step_key),
        )
        db.add(row)
        rows.append(row)
    work_order.plan_revision = revision
    await append_event(
        db,
        work_order.id,
        "plan.created",
        actor=actor,
        payload={"plan_id": str(plan.id), "revision": revision, "steps": len(rows)},
    )
    await transition_work_order(db, work_order, "ready", actor=actor)
    await db.flush()
    return plan, rows


async def claim_ready_step(
    db: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int = 120,
    work_order_id: uuid.UUID | None = None,
) -> tuple[WorkOrder, WorkStep, WorkStepAttempt] | None:
    now = utcnow()
    query = (
        select(WorkStep)
        .join(WorkOrder, WorkOrder.id == WorkStep.work_order_id)
        .join(WorkPlan, WorkPlan.id == WorkStep.plan_id)
        .where(
            WorkPlan.status == "active",
            WorkStep.state.in_(["ready", "retry_wait"]),
            WorkOrder.status.in_(["ready", "running"]),
            (WorkStep.next_attempt_at.is_(None) | (WorkStep.next_attempt_at <= now)),
            (WorkStep.lease_expires_at.is_(None) | (WorkStep.lease_expires_at <= now)),
        )
        .order_by(WorkOrder.priority.desc(), WorkStep.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if work_order_id is not None:
        query = query.where(WorkStep.work_order_id == work_order_id)
    step = (await db.execute(query)).scalar_one_or_none()
    if step is None:
        return None
    order = await db.get(WorkOrder, step.work_order_id, with_for_update=True)
    if order is None:
        return None
    if step.state == "retry_wait":
        await transition_step(db, step, "ready", actor="scheduler")
    await transition_step(db, step, "running", actor=worker_id)
    if order.status == "ready":
        await transition_work_order(db, order, "running", actor=worker_id)
    lease_expires = now + timedelta(seconds=max(10, lease_seconds))
    step.lease_owner = worker_id
    step.lease_expires_at = lease_expires
    order.lease_owner = worker_id
    order.lease_expires_at = lease_expires
    step.attempt_count += 1
    attempt = WorkStepAttempt(
        step_id=step.id,
        attempt_no=step.attempt_count,
        worker_id=worker_id,
        input_=step.input_,
        heartbeat_at=now,
    )
    db.add(attempt)
    await append_event(
        db,
        order.id,
        "step.claimed",
        actor=worker_id,
        payload={
            "step_id": str(step.id),
            "attempt": step.attempt_count,
            "lease_expires_at": lease_expires.isoformat(),
        },
    )
    await db.flush()
    return order, step, attempt


async def reclaim_expired_leases(db: AsyncSession, *, actor: str = "scheduler") -> int:
    """Recover steps whose worker disappeared without finishing its attempt."""
    now = utcnow()
    steps = list(
        (
            await db.execute(
                select(WorkStep)
                .where(
                    WorkStep.state == "running",
                    WorkStep.lease_expires_at.is_not(None),
                    WorkStep.lease_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for step in steps:
        attempt = (
            await db.execute(
                select(WorkStepAttempt)
                .where(
                    WorkStepAttempt.step_id == step.id,
                    WorkStepAttempt.status == "running",
                )
                .order_by(WorkStepAttempt.attempt_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        error = {"code": "lease_expired", "message": "Worker heartbeat expired"}
        if attempt is not None:
            attempt.status = "abandoned"
            attempt.error = error
            attempt.finished_at = now
        step.last_error = error
        step.lease_owner = None
        step.lease_expires_at = None
        order = await db.get(WorkOrder, step.work_order_id, with_for_update=True)
        if order is None:
            continue
        order.lease_owner = None
        order.lease_expires_at = None
        if step.attempt_count < step.max_attempts:
            await transition_step(db, step, "retry_wait", actor=actor, payload={"error": error})
            step.next_attempt_at = now
            if order.status == "running":
                await transition_work_order(db, order, "ready", actor=actor)
        else:
            await transition_step(db, step, "failed", actor=actor, payload={"error": error})
            order.blocker = {"code": "lease_expired", "step_id": str(step.id)}
            if order.status == "running":
                max_replans = max(0, int((order.budgets or {}).get("max_replans", 2)))
                target = "replanning" if order.plan_revision <= max_replans else "blocked"
                await transition_work_order(db, order, target, actor=actor)
    await db.flush()
    return len(steps)


async def complete_attempt(
    db: AsyncSession,
    *,
    order: WorkOrder,
    step: WorkStep,
    attempt: WorkStepAttempt,
    output: dict[str, Any],
    actor: str,
) -> None:
    now = utcnow()
    attempt.status = "succeeded"
    attempt.output = output
    attempt.finished_at = now
    attempt.heartbeat_at = now
    step.output = output
    await transition_step(db, step, "succeeded", actor=actor)
    await append_event(
        db,
        order.id,
        "step.output_recorded",
        actor=actor,
        payload={"step_id": str(step.id), "attempt_id": str(attempt.id)},
    )


async def promote_ready_dependents(
    db: AsyncSession,
    *,
    order: WorkOrder,
    plan_id: uuid.UUID,
    actor: str = "scheduler",
) -> bool:
    """Release DAG steps whose dependencies succeeded.

    Returns True while the active plan still has unfinished steps.
    """
    steps = list(
        (
            await db.execute(
                select(WorkStep)
                .where(WorkStep.plan_id == plan_id)
                .with_for_update()
            )
        ).scalars()
    )
    states = {step.step_key: step.state for step in steps}
    for step in steps:
        if step.state == "pending" and all(
            states.get(dependency) == "succeeded" for dependency in step.depends_on
        ):
            await transition_step(db, step, "ready", actor=actor)
    unfinished = any(step.state != "succeeded" for step in steps)
    if unfinished and order.status == "running":
        await transition_work_order(db, order, "ready", actor=actor)
    return unfinished


async def fail_attempt(
    db: AsyncSession,
    *,
    order: WorkOrder,
    step: WorkStep,
    attempt: WorkStepAttempt,
    error: dict[str, Any],
    retryable: bool,
    actor: str,
) -> None:
    now = utcnow()
    attempt.status = "failed"
    attempt.error = error
    attempt.finished_at = now
    attempt.heartbeat_at = now
    step.last_error = error
    if retryable and step.attempt_count < step.max_attempts:
        await transition_step(db, step, "retry_wait", actor=actor, payload={"error": error})
        base = int((step.retry_policy or {}).get("base_seconds", 5))
        maximum = int((step.retry_policy or {}).get("max_seconds", 300))
        delay = min(maximum, base * (2 ** max(0, step.attempt_count - 1)))
        step.next_attempt_at = now + timedelta(seconds=delay)
        await transition_work_order(db, order, "ready", actor=actor, payload={"retry_in": delay})
    else:
        await transition_step(db, step, "failed", actor=actor, payload={"error": error})
        order.blocker = {"code": "step_failed", "step_id": str(step.id), "error": error}
        max_replans = max(0, int((order.budgets or {}).get("max_replans", 2)))
        target = "replanning" if order.plan_revision <= max_replans else "blocked"
        await transition_work_order(db, order, target, actor=actor)


async def verify_nonempty_result(
    db: AsyncSession,
    *,
    order: WorkOrder,
    step: WorkStep,
    actor: str = "deterministic-verifier",
) -> bool:
    await transition_work_order(db, order, "verifying", actor=actor)
    output = step.output or {}
    text = str(output.get("text") or output.get("result_summary") or "").strip()
    has_result = bool(text) and not output.get("errors")
    unresolved_required: list[str] = []
    failed_required: list[str] = []
    criteria = list(
        (
            await db.execute(
                select(WorkAcceptanceCriterion).where(
                    WorkAcceptanceCriterion.work_order_id == order.id
                )
            )
        ).scalars()
    )
    for criterion in criteria:
        predicate = criterion.predicate or {}
        predicate_type = str(predicate.get("type") or "")
        verdict_ok: bool | None = None
        reason = "criterion requires an independent verifier"
        evidence_payload: dict[str, Any] = {"predicate_type": predicate_type}
        if predicate_type == "nonempty_result":
            verdict_ok = has_result
            reason = "non-empty error-free result" if has_result else "result is empty or errored"
            evidence_payload.update(
                {"text_length": len(text), "has_errors": bool(output.get("errors"))}
            )
        elif predicate_type == "min_length":
            minimum = max(0, int(predicate.get("value", 1)))
            verdict_ok = len(text) >= minimum and not output.get("errors")
            reason = f"result length {len(text)}; required at least {minimum}"
            evidence_payload.update({"text_length": len(text), "minimum": minimum})
        elif predicate_type == "contains":
            needle = str(predicate.get("value") or "")
            verdict_ok = bool(needle) and needle.casefold() in text.casefold()
            reason = "required text found" if verdict_ok else "required text not found"
            evidence_payload["needle"] = needle
        elif predicate_type == "regex":
            pattern = str(predicate.get("pattern") or "")
            try:
                verdict_ok = bool(pattern) and re.search(pattern, text) is not None
                reason = (
                    "regular expression matched"
                    if verdict_ok
                    else "regular expression did not match"
                )
            except re.error as exc:
                verdict_ok = False
                reason = f"invalid regular expression: {exc}"
            evidence_payload["pattern"] = pattern

        if verdict_ok is not None:
            criterion.status = "passed" if verdict_ok else "failed"
            criterion.verdict = {
                "ok": verdict_ok,
                "reason": reason,
            }
            criterion.verified_at = utcnow()
            criterion.verified_by = actor
            evidence = WorkEvidence(
                work_order_id=order.id,
                criterion_id=criterion.id,
                step_id=step.id,
                evidence_type="deterministic_check",
                source="work_step.output",
                payload=evidence_payload,
                verifier_status="passed" if verdict_ok else "failed",
            )
            db.add(evidence)
            if criterion.required and not verdict_ok:
                failed_required.append(criterion.criterion_key)
        elif criterion.required:
            unresolved_required.append(criterion.criterion_key)
    await db.flush()
    if has_result and not failed_required and not unresolved_required:
        order.result_summary = text[:8000]
        await transition_work_order(db, order, "completed", actor=actor)
    elif unresolved_required:
        order.blocker = {
            "code": "independent_verification_required",
            "criteria": unresolved_required,
        }
        await transition_work_order(db, order, "blocked", actor=actor)
    else:
        order.blocker = {
            "code": "verification_failed",
            "criteria": failed_required,
            "reason": "acceptance criteria did not pass",
        }
        await transition_work_order(db, order, "blocked", actor=actor)
    return order.status == "completed"


async def assert_completion_allowed(db: AsyncSession, work_order_id: uuid.UUID) -> None:
    failed_required = int(
        (
            await db.execute(
                select(func.count())
                .select_from(WorkAcceptanceCriterion)
                .where(
                    WorkAcceptanceCriterion.work_order_id == work_order_id,
                    WorkAcceptanceCriterion.required.is_(True),
                    WorkAcceptanceCriterion.status != "passed",
                )
            )
        ).scalar_one()
    )
    if failed_required:
        raise WorkStateError(
            f"Work order cannot complete: {failed_required} required criteria have not passed"
        )


async def apply_approval_decision(
    db: AsyncSession,
    *,
    work_order_id: uuid.UUID,
    step_id: uuid.UUID,
    approval_id: uuid.UUID,
    approved: bool,
    actor: str,
    action_digest: str | None = None,
) -> None:
    """Resume or block a work step after its durable approval is decided."""
    order = await db.get(WorkOrder, work_order_id, with_for_update=True)
    step = await db.get(WorkStep, step_id, with_for_update=True)
    if order is None or step is None or step.work_order_id != work_order_id:
        raise WorkStateError("Approval refers to a missing work order or step")
    if order.status != "waiting_approval" or step.state != "waiting_approval":
        raise WorkStateError("Work order is no longer waiting for this approval")
    if approved:
        step_input = dict(step.input_ or {})
        step_input["approval"] = {
            "approval_id": str(approval_id),
            "action_digest": action_digest,
            "approved_by": actor,
        }
        step.input_ = step_input
        await transition_step(
            db,
            step,
            "ready",
            actor=actor,
            payload={"approval_id": str(approval_id)},
        )
        await transition_work_order(
            db,
            order,
            "ready",
            actor=actor,
            payload={"approval_id": str(approval_id)},
        )
    else:
        await transition_step(
            db,
            step,
            "failed",
            actor=actor,
            payload={"approval_id": str(approval_id), "decision": "rejected"},
        )
        order.blocker = {
            "code": "approval_rejected",
            "approval_id": str(approval_id),
            "step_id": str(step.id),
        }
        await transition_work_order(
            db,
            order,
            "blocked",
            actor=actor,
            payload={"approval_id": str(approval_id)},
        )


async def record_verifier_verdict(
    db: AsyncSession,
    *,
    order: WorkOrder,
    criterion: WorkAcceptanceCriterion,
    ok: bool,
    reason: str,
    evidence_payload: dict[str, Any],
    actor: str,
) -> bool:
    """Record an independent verdict and complete only when every gate passes."""
    if actor == order.owner_key:
        raise WorkStateError("Work-order owner cannot independently verify its own result")
    if criterion.work_order_id != order.id:
        raise WorkStateError("Criterion does not belong to the work order")
    criterion.status = "passed" if ok else "failed"
    criterion.verdict = {"ok": ok, "reason": reason}
    criterion.verified_at = utcnow()
    criterion.verified_by = actor
    db.add(
        WorkEvidence(
            work_order_id=order.id,
            criterion_id=criterion.id,
            evidence_type="independent_verdict",
            source=actor,
            payload={"reason": reason, **evidence_payload},
            verifier_status="passed" if ok else "failed",
        )
    )
    await append_event(
        db,
        order.id,
        "criterion.verified",
        actor=actor,
        payload={
            "criterion_id": str(criterion.id),
            "criterion_key": criterion.criterion_key,
            "ok": ok,
        },
    )
    await db.flush()
    if not ok:
        order.blocker = {
            "code": "verification_failed",
            "criterion": criterion.criterion_key,
            "reason": reason,
        }
        return False
    try:
        await assert_completion_allowed(db, order.id)
    except WorkStateError:
        return False
    incomplete_steps = int(
        (
            await db.execute(
                select(func.count())
                .select_from(WorkStep)
                .join(WorkPlan, WorkPlan.id == WorkStep.plan_id)
                .where(
                    WorkStep.work_order_id == order.id,
                    WorkPlan.status == "active",
                    WorkStep.state != "succeeded",
                )
            )
        ).scalar_one()
    )
    if incomplete_steps:
        return False
    final_step = (
        await db.execute(
            select(WorkStep)
            .where(WorkStep.work_order_id == order.id, WorkStep.state == "succeeded")
            .order_by(WorkStep.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if final_step is not None:
        final_output = final_step.output or {}
        order.result_summary = str(
            final_output.get("text") or final_output.get("result_summary") or ""
        )[:8000]
    if order.status == "blocked":
        await transition_work_order(db, order, "verifying", actor=actor)
    elif order.status == "running":
        await transition_work_order(db, order, "verifying", actor=actor)
    if order.status == "verifying":
        order.blocker = None
        await transition_work_order(db, order, "completed", actor=actor)
        return True
    return order.status == "completed"
