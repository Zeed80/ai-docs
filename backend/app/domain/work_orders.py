"""Durable autonomous work-order state machine and persistence services."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

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
    "ready": frozenset({"running", "waiting_approval", "replanning", "blocked", "canceled"}),
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
    # Б11: "waiting_external" (previously reserved, unused) now also covers a
    # parent WorkOrder waiting on child WorkOrders spawned by a decompose
    # step — promote_waiting_parents routes it through the same "verifying"
    # -> completed/blocked path verify_nonempty_result already uses for a
    # normal step, once every child reaches a terminal state.
    "waiting_external": frozenset({"ready", "replanning", "blocked", "canceled", "verifying"}),
    "verifying": frozenset({"completed", "replanning", "blocked", "failed", "canceled"}),
    "replanning": frozenset({"ready", "blocked", "failed", "canceled"}),
    # Ф4-re (AGENT_AUTONOMY_ROADMAP.md, found live on the persistence
    # re-verification pilot): "replanning" added here. A semantic-verifier
    # rejection (record_verifier_verdict) lands the order on "blocked" via
    # the earlier "independent_verification_required" hold — without this,
    # record_verifier_verdict's own bounded-replan attempt (mirroring
    # verify_nonempty_result's deterministic-failure branch) had nowhere
    # legal to go and silently no-op'ed, so the agent never got a second
    # try when its OWN synthesis was judged insufficient — only step-level
    # execution failures ever consumed the max_replans budget.
    "blocked": frozenset({"scoping", "planning", "ready", "verifying", "replanning", "canceled"}),
    "failed": frozenset({"planning", "ready", "canceled"}),
    "completed": frozenset(),
    "canceled": frozenset(),
}

STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"ready", "skipped", "canceled"}),
    "ready": frozenset({"running", "waiting_approval", "canceled"}),
    "running": frozenset({"succeeded", "retry_wait", "waiting_approval", "failed", "canceled"}),
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
    if is_exploratory(work_order) and target in _EXPLORATORY_PROGRESS_NOTIFY_STATUSES:
        # Ф4 (AGENT_AUTONOMY_ROADMAP.md): progress visibility for long-running
        # exploratory WorkOrders — an hours-long unattended task going silent
        # until it finishes is exactly the "black box" the roadmap's Ф2.B
        # design section warned against. Reuses Ф0's Notification
        # infrastructure (same create_notification, same source_task
        # calibration hook) rather than a parallel channel. Every other
        # WorkOrder (the overwhelming majority — short capability-mode tasks)
        # is unaffected: this only fires for constraints.mode="exploratory".
        # Best-effort — a notification failure must never break a status
        # transition that has already been persisted.
        try:
            await _notify_exploratory_progress(db, work_order, target)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "exploratory_progress_notification_failed",
                work_order_id=str(work_order.id),
                error=str(exc),
            )
    return work_order


_EXPLORATORY_PROGRESS_NOTIFY_STATUSES = frozenset({"verifying", "blocked", "completed", "failed"})


async def _notify_exploratory_progress(db: AsyncSession, order: WorkOrder, target: str) -> None:
    from app.db.models import NotificationType
    from app.services.notifications import create_notification

    titles = {
        "verifying": "Проверка результата поручения",
        "blocked": "Поручение остановлено",
        "completed": "Поручение завершено",
        "failed": "Поручение не выполнено",
    }
    body = f"«{order.objective[:200]}» — статус: {target}."
    if target == "blocked" and order.blocker:
        reason = order.blocker.get("code") or order.blocker.get("message") or order.blocker
        body += f" Причина: {reason}."
    await create_notification(
        db,
        user_sub=order.owner_key,
        type=NotificationType.system,
        title=titles.get(target, "Обновление поручения"),
        body=body,
        entity_type="work_order",
        entity_id=order.id,
        action_url=f"/work-orders/{order.id}",
        source_task="workorder.progress",
    )


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


def is_exploratory(order: WorkOrder) -> bool:
    """Ф1.A (AGENT_AUTONOMY_ROADMAP.md): the mode flag for an open-ended
    WorkOrder (e.g. "find and structure supplier catalogs") whose full scope
    isn't known up front, as opposed to the default capability-grounded mode
    whose DAG is bounded at planning time. A plain ``constraints`` key, not a
    new column — ``constraints``/``budgets``/``metadata_`` are already
    unstructured JSON, and every existing WorkOrder (constraints defaulting to
    ``{}``) reads as non-exploratory with zero migration. Read by the planner
    (generate_capability_plan) to steer prompting and by callers deciding
    which acceptance-criteria set (default vs exploratory_acceptance_criteria)
    to pass to create_work_order.
    """
    return str((order.constraints or {}).get("mode") or "") == "exploratory"


# Ф4 (AGENT_AUTONOMY_ROADMAP.md, user feedback 2026-08-20): a bounded
# capability-grounded order's replans are genuine failure-recovery attempts —
# 2 is a reasonable default ceiling before conceding to a human. An
# exploratory order's replans are its *continue-working* mechanism (Ф1.A:
# "rely on replanning ... to continue once this horizon finishes" — each
# replan is the next horizon, not just an error retry), so the same small
# default silently strangled persistence: the live Ф4 pilot spent most of
# its manually-raised budget of 6 on transient infra bugs (now fixed, see
# AGENT_AUTONOMY_ROADMAP.md Ф4 findings) rather than genuine strategy
# exhaustion, and still ran out before the agent could try alternate
# approaches on its last open item. The real ceiling on an exploratory
# order's persistence should be its wall-clock/cost/tool-call budgets
# (Ф1.C — resources actually spent), not an arbitrary small count of DAG
# rebuilds; this default is generous specifically so plan-revision count
# essentially never becomes the binding constraint before those do. An
# order that explicitly sets budgets.max_replans always wins regardless of
# mode — this is only the fallback when it's unset.
_DEFAULT_MAX_REPLANS = 2
_DEFAULT_MAX_REPLANS_EXPLORATORY = 30


def _max_replans_for(order: WorkOrder) -> int:
    configured = (order.budgets or {}).get("max_replans")
    if configured is not None:
        return max(0, int(configured))
    return _DEFAULT_MAX_REPLANS_EXPLORATORY if is_exploratory(order) else _DEFAULT_MAX_REPLANS


def exploratory_acceptance_criteria() -> list[dict[str, Any]]:
    """Ф1.D: the honest-coverage acceptance-criteria pair for an exploratory
    WorkOrder — pass as create_work_order's ``acceptance_criteria`` instead of
    its default result_present/nonempty_result pair, which demands a single
    complete result an open-ended search can never promise.

    Both required, both going through the *existing* fail-closed criterion
    machinery unchanged (verify_nonempty_result / verify_semantic_criteria /
    record_verifier_verdict) — no new verdict path, no FSM change:

    - ``coverage_report`` (deterministic, verify_nonempty_result): the plan's
      final step output must be ``{"text": <summary>, "coverage":
      {"covered": [...], "partial": [...], "not_found": [...]}}``. Checks
      *shape*, not exhaustiveness.
    - ``honest_not_found`` (independent semantic verifier): every
      ``not_found`` entry must be backed by a real attempt/evidence, not
      fabricated. No new verifier code — the existing verifier already judges
      from each criterion's own ``description`` plus the supplied step
      outputs, so stating the expectation there is enough.

    Description text tightened 2026-08-20 (user feedback after the Ф4
    pilot): a criterion that only asked for "one real attempt" let the model
    write off a source after its first failure and call that honest — the
    goal is completing the objective, not an honestly-worded early exit. The
    verifier must now reject a not_found entry backed by a single attempt.
    """
    return [
        {
            "criterion_key": "coverage_report",
            "description": (
                "Финальный шаг вернул структурированный отчёт о покрытии: "
                "output.coverage = {covered: [...], partial: [...], "
                "not_found: [...]} — списки полностью найденного, частично "
                "найденного и не найденного, плюс краткое текстовое summary."
            ),
            "kind": "artifact",
            "predicate": {"type": "coverage_report"},
            "required": True,
        },
        {
            "criterion_key": "honest_not_found",
            "description": (
                "Цель — реально выполнить задачу, а не подобрать честную "
                "формулировку отказа. Каждый пункт not_found в итоговом "
                "отчёте подкреплён НЕСКОЛЬКИМИ разными зафиксированными "
                "попытками (видно по succeeded/failed-шагам плана: разные "
                "запросы, источники, инструменты или подходы) — одна "
                "неудачная попытка НЕ является достаточным основанием "
                "считать пункт not_found; такой отчёт должен провалить "
                "критерий, даже если формально не выдуман. Также "
                "провалить, если очевидная альтернативная стратегия "
                "(другой запрос/источник/capability) не была опробована, "
                "хотя была доступна."
            ),
            "kind": "semantic",
            "predicate": {"type": "honest_not_found"},
            "required": True,
        },
    ]


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
                "success_predicate": success_predicate or {"type": "no_error_and_nonempty_output"},
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
        verification_plan=verification_plan or {"mode": "deterministic_then_independent"},
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
                item.get("success_predicate") or {"type": "no_error_and_nonempty_output"}
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


async def enforce_budgets(db: AsyncSession, *, actor: str = "scheduler") -> int:
    """Б15: block any ready/running WorkOrder that exceeded a configured budget.

    Runs as a periodic housekeeping pass (mirrors reclaim_expired_leases) —
    NOT inside claim_ready_step's SKIP LOCKED query — so a work order this
    blocks simply stops matching claim_ready_step's ``status.in_(["ready",
    "running"])`` filter on the next dispatch tick, with zero change to that
    hot claim path. Only WorkOrders with an explicit budget field set are
    checked; nothing is enforced by default (see A4/Б15 on defaults).

    Ф1.C (AGENT_AUTONOMY_ROADMAP.md): extends the original token_budget check
    with three more dimensions an open-ended exploratory WorkOrder needs and a
    capability-grounded one doesn't (its DAG is bounded up front) —
    ``max_cost_usd`` (sum of WorkStepAttempt.cost_usd, for provider calls that
    report cost instead of/alongside tokens), ``max_wall_clock_seconds``
    (elapsed since the order started running), and ``max_tool_calls`` (count
    of WorkToolCall rows, i.e. every capability
    invocation regardless of kind — web fetch, search, whatever). A
    capability-specific "web requests only" ceiling is deliberately not built
    here: there's no web-facing capability yet to scope it to (that lands in
    Ф2 alongside ComputerUseGrant wiring) — ``max_tool_calls`` is the honest
    generic mechanism available today, not a placeholder for a narrower one.
    Checked in this fixed order per order (first budget that's actually
    exceeded wins the blocker reason) so a caller sees why it stopped rather
    than an arbitrary pick among several exceeded budgets.
    """
    # Filtered in Python, not a JSON-path SQL predicate on `budgets` — matches
    # how every other budget field in this module is read (max_replans,
    # timeout_seconds) and stays portable across the Postgres/SQLite backends
    # this project runs against, instead of relying on a JSON operator that
    # behaves differently between them.
    candidates = list(
        (
            await db.execute(select(WorkOrder).where(WorkOrder.status.in_(["ready", "running"])))
        ).scalars()
    )
    blocked = 0
    for order in candidates:
        budgets = order.budgets or {}
        blocker: dict[str, Any] | None = None

        token_budget = budgets.get("token_budget")
        if token_budget is not None:
            spent = (
                await db.execute(
                    select(func.coalesce(func.sum(WorkStepAttempt.tokens_used), 0))
                    .join(WorkStep, WorkStep.id == WorkStepAttempt.step_id)
                    .where(WorkStep.work_order_id == order.id)
                )
            ).scalar_one()
            if spent > int(token_budget):
                blocker = {
                    "code": "token_budget_exceeded",
                    "token_budget": int(token_budget),
                    "tokens_spent": int(spent),
                }

        max_cost_usd = budgets.get("max_cost_usd")
        if blocker is None and max_cost_usd is not None:
            cost_spent = (
                await db.execute(
                    select(func.coalesce(func.sum(WorkStepAttempt.cost_usd), 0.0))
                    .join(WorkStep, WorkStep.id == WorkStepAttempt.step_id)
                    .where(WorkStep.work_order_id == order.id)
                )
            ).scalar_one()
            if float(cost_spent) > float(max_cost_usd):
                blocker = {
                    "code": "cost_budget_exceeded",
                    "max_cost_usd": float(max_cost_usd),
                    "cost_spent_usd": round(float(cost_spent), 4),
                }

        max_wall_clock = budgets.get("max_wall_clock_seconds")
        if blocker is None and max_wall_clock is not None and order.started_at is not None:
            elapsed = (utcnow() - order.started_at).total_seconds()
            if elapsed > float(max_wall_clock):
                blocker = {
                    "code": "wall_clock_budget_exceeded",
                    "max_wall_clock_seconds": float(max_wall_clock),
                    "elapsed_seconds": round(elapsed, 1),
                }

        max_tool_calls = budgets.get("max_tool_calls")
        if blocker is None and max_tool_calls is not None:
            from app.db.models import WorkToolCall

            call_count = (
                await db.execute(
                    select(func.count())
                    .select_from(WorkToolCall)
                    .where(WorkToolCall.work_order_id == order.id)
                )
            ).scalar_one()
            if call_count > int(max_tool_calls):
                blocker = {
                    "code": "tool_call_budget_exceeded",
                    "max_tool_calls": int(max_tool_calls),
                    "tool_calls_made": int(call_count),
                }

        if blocker is None:
            continue
        order.blocker = blocker
        await transition_work_order(db, order, "blocked", actor=actor)
        blocked += 1
    if blocked:
        await db.flush()
    return blocked


async def enter_waiting_for_children(
    db: AsyncSession, *, order: WorkOrder, actor: str = "scheduler"
) -> None:
    """Б11: a decompose step just succeeded — the parent isn't done, it's
    waiting on the children it spawned. See promote_waiting_parents for the
    other half (what happens once they all finish)."""
    await transition_work_order(db, order, "waiting_external", actor=actor)


def _blocker_reason(blocker: dict[str, Any] | None) -> str | None:
    """Ф4: the most specific human-readable reason in a WorkOrder's blocker,
    for coverage reporting (promote_waiting_parents below). Blocker shapes
    vary by source — fail_attempt's terminal-failure branch wraps the real
    cause one level deeper under "error" (``{"code": "step_failed", "error":
    {"code": "no_grant", ...}}``), while enforce_budgets/verify_nonempty_result
    put it straight at the top level (``{"code": "token_budget_exceeded", ...}``)
    — surfacing the generic "step_failed" wrapper when a real cause sits right
    underneath it would be technically honest but needlessly vague.
    """
    if not blocker:
        return None
    code = blocker.get("code")
    if code == "step_failed" and isinstance(blocker.get("error"), dict):
        inner = blocker["error"]
        return inner.get("code") or inner.get("message") or code
    return code or blocker.get("message")


async def promote_waiting_parents(db: AsyncSession, *, actor: str = "scheduler") -> int:
    """Б11: resolve any decompose-parent whose children are all done.

    Periodic housekeeping — same pattern as reclaim_expired_leases/
    enforce_budgets, called from _dispatch_ready_work. Scoped to orders that
    actually have children (parent_id points at them): "waiting_external" is
    a general-purpose reserved status, not decompose-specific, so an order
    sitting in it for some other future reason with no children is left
    alone rather than assumed to be a decompose parent.

    Deliberately routes through verify_nonempty_result — the same
    completed/blocked decision every other WorkOrder goes through — instead
    of a bespoke completed-or-failed branch here. Two reasons: (1) every
    order gets a default required "result_present" criterion at creation
    (create_work_order) that only verify_nonempty_result knows how to mark
    passed, so skipping it left a decompose-parent permanently unable to
    reach "completed" (found by the test for this function — the parent's
    own default criterion was still unresolved); (2) if the objective that
    produced this decompose plan had explicit acceptance_criteria of its
    own, they still get evaluated for real instead of being silently assumed
    satisfied because the children succeeded.
    """
    candidates = list(
        (
            await db.execute(select(WorkOrder).where(WorkOrder.status == "waiting_external"))
        ).scalars()
    )
    promoted = 0
    for order in candidates:
        children = list(
            (await db.execute(select(WorkOrder).where(WorkOrder.parent_id == order.id))).scalars()
        )
        if not children:
            continue
        if any(child.status not in TERMINAL_WORK_STATUSES for child in children):
            continue  # still waiting on at least one
        decompose_step = (
            await db.execute(
                select(WorkStep).where(
                    WorkStep.work_order_id == order.id, WorkStep.kind == "decompose"
                )
            )
        ).scalar_one_or_none()
        if decompose_step is None:
            continue  # waiting_external for some other, non-decompose reason

        succeeded = [c for c in children if c.status == "completed"]
        lines = [f"Дочерних поручений: {len(children)}, завершено успешно: {len(succeeded)}."]
        for child in children:
            summary = child.result_summary or f"[{child.status}]"
            lines.append(f"— {child.objective}: {summary}")
        aggregated_text = "\n".join(lines)

        # verify_nonempty_result's has_result = bool(text) and not errors —
        # zero successful children must read as "no result", not merely "the
        # explanation text happens to be non-empty" (it always is, it lists
        # every child's failure).
        decompose_step.output = {
            "text": aggregated_text,
            **({} if succeeded else {"errors": ["all children failed"]}),
        }
        if is_exploratory(order):
            # Ф4 (AGENT_AUTONOMY_ROADMAP.md): found integrating Ф1-Ф3 —
            # exploratory_acceptance_criteria()'s coverage_report predicate
            # needs output.coverage = {covered, partial, not_found}, but this
            # function only ever produced a human-readable text summary. A
            # decompose-fan-out (one child per discovered supplier/source —
            # the exploratory planner's own preferred pattern, see the
            # planner prompt addendum in work_planning.py) could never
            # satisfy that criterion without this: no other step produces
            # a structured coverage object for the parent. Built from the
            # same per-child data the text summary above already uses, so
            # it can't disagree with what a human reads in that summary.
            decompose_step.output["coverage"] = {
                "covered": [c.objective for c in succeeded],
                "partial": [],
                "not_found": [
                    {"item": c.objective, "reason": _blocker_reason(c.blocker) or c.status}
                    for c in children
                    if c.status != "completed"
                ],
            }
        try:
            await verify_nonempty_result(db, order=order, step=decompose_step, actor=actor)
        except WorkStateError as exc:
            # e.g. an explicit acceptance criterion on the parent itself
            # needs an independent verifier — correctly refuses a false
            # "completed", stays in waiting_external for a human/next tick
            # to resolve rather than crashing the rest of this batch.
            logger.warning(
                "promote_waiting_parents_blocked", order_id=str(order.id), error=str(exc)
            )
            continue
        promoted += 1
    if promoted:
        await db.flush()
    return promoted


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
            # Ф4 (AGENT_AUTONOMY_ROADMAP.md): found live on the Ф4 pilot —
            # gating this on `order.status == "running"` silently dropped the
            # order-level transition whenever the lease actually expired
            # (>=120s later) with the order having already moved on to
            # "ready" in the meantime (e.g. a sibling step finished first and
            # promote_ready_dependents flipped it back). The order's blocker
            # got recorded but its status never changed, so a permanently
            # failed step with dependents left the whole WorkOrder stuck
            # forever — dispatch_ready ticks with nothing claimable and
            # nothing re-evaluates it. Checking WORK_TRANSITIONS instead of
            # one specific source status covers every state that can
            # legally reach the target (matches what transition_work_order
            # itself would accept) while still no-op'ing for genuinely
            # terminal orders (completed/canceled) instead of raising.
            if "ready" in WORK_TRANSITIONS.get(order.status, frozenset()):
                await transition_work_order(db, order, "ready", actor=actor)
        else:
            await transition_step(db, step, "failed", actor=actor, payload={"error": error})
            order.blocker = {"code": "lease_expired", "step_id": str(step.id)}
            max_replans = _max_replans_for(order)
            target = "replanning" if order.plan_revision <= max_replans else "blocked"
            if target in WORK_TRANSITIONS.get(order.status, frozenset()):
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
    # Б15: only agent_turn steps populate this today (tokens_used from
    # AgentSession.total_tokens, Ollama calls only — see agent_loop.py's
    # _accumulate_usage). Capability steps have no LLM cost of their own to
    # attribute here; a capability that internally triggers model usage
    # (e.g. documents.summarize) is not tracked by this mechanism.
    tokens_used = output.get("tokens_used") if isinstance(output, dict) else None
    if isinstance(tokens_used, dict):
        attempt.tokens_used = int(tokens_used.get("input_tokens") or 0) + int(
            tokens_used.get("output_tokens") or 0
        )
    # Ф1.C: symmetric with tokens_used above — an executor that knows its own
    # USD cost (a cloud-provider call billed by the request, a paid web-search
    # API, ...) can report it the same way, and max_cost_usd (enforce_budgets)
    # will aggregate it. Same honest caveat as tokens_used: nothing populates
    # this today (Ollama is free/local), so max_cost_usd only starts blocking
    # once a real cost-reporting executor exists — but the aggregation itself
    # is wired now, not a dead column waiting for one later.
    cost_usd = output.get("cost_usd") if isinstance(output, dict) else None
    if isinstance(cost_usd, (int, float)):
        attempt.cost_usd = float(cost_usd)
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
            await db.execute(select(WorkStep).where(WorkStep.plan_id == plan_id).with_for_update())
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


async def find_active_plan_succeeded_steps(
    db: AsyncSession, *, limit: int = 100
) -> list[uuid.UUID]:
    """Succeeded steps of a *currently active* plan, for a running order.

    Feeds the periodic verify_work_step dispatch (_dispatch_ready_work).

    Ф4 (AGENT_AUTONOMY_ROADMAP.md): found live on the Ф4 pilot — the query
    this replaced had no plan filter at all, so a WorkOrder that replanned
    multiple times kept re-selecting already-succeeded steps from
    *superseded* plans forever (nothing here or in verify_completed_step
    ever stops re-querying a step just because it's already been verified
    once). verify_completed_step then calls
    promote_ready_dependents(plan_id=step.plan_id) for that stale plan,
    which recomputes "unfinished" from the OLD plan's own steps (still
    counting its permanently-failed/never-promotable siblings) and flips
    the order back to "ready" — stomping on a completely different step
    that was actively "running" in the *current* plan at that exact
    moment. claim_ready_step already scopes its own query to
    ``WorkPlan.status == "active"`` for the same reason; this needs the
    identical join/filter.

    Deliberately scoped to ``status == "running"``, not also "ready": a
    "ready" order whose active plan is done stepping but never got
    verified is a *separate* starvation case (see
    unstick_ready_orders_with_stalled_active_plan) — nudging it back to
    "running" once there is exactly right, but broadening this query
    itself to "ready" would re-query and re-dispatch verify_work_step for
    every already-succeeded step on every 5s tick for the entire time an
    order sits "ready" between step claims (its normal resting state for
    most of a multi-step plan's life), not just the one genuinely stalled
    case.
    """
    rows = (
        await db.execute(
            select(WorkStep.id)
            .join(WorkOrder, WorkOrder.id == WorkStep.work_order_id)
            .join(WorkPlan, WorkPlan.id == WorkStep.plan_id)
            .where(
                WorkStep.state == "succeeded",
                WorkOrder.status == "running",
                WorkPlan.status == "active",
            )
            .limit(limit)
        )
    ).scalars()
    return list(rows)


async def unstick_ready_orders_with_stalled_active_plan(
    db: AsyncSession, *, actor: str = "scheduler"
) -> int:
    """Recover a "ready" order whose active plan finished stepping but was
    never handed back for verification.

    Ф4 (AGENT_AUTONOMY_ROADMAP.md): found live on the Ф4 pilot, right after
    fixing find_active_plan_succeeded_steps above — a succeeded step that
    was the *last* claimable one in its plan leaves the order "ready" with
    nothing left for claim_ready_step to pick up (that is the only thing
    that would otherwise flip it back to "running"), so the succeeded step
    never gets verified and the order is stuck in "ready" forever. Scoped
    narrowly on purpose: only orders whose active plan has zero steps left
    in any in-flight state (ready/retry_wait/running/pending/
    waiting_approval) *and* at least one succeeded step qualify — an order
    resting in "ready" with real pending work is untouched, so this adds
    no extra churn to the common case.
    """
    in_flight_states = ("ready", "retry_wait", "running", "pending", "waiting_approval")
    stalled_plan_ids = list(
        (
            await db.execute(
                select(WorkPlan.id)
                .join(WorkOrder, WorkOrder.id == WorkPlan.work_order_id)
                .where(
                    WorkOrder.status == "ready",
                    WorkPlan.status == "active",
                    ~exists(
                        select(1).where(
                            WorkStep.plan_id == WorkPlan.id,
                            WorkStep.state.in_(in_flight_states),
                        )
                    ),
                    exists(
                        select(1).where(
                            WorkStep.plan_id == WorkPlan.id,
                            WorkStep.state == "succeeded",
                        )
                    ),
                )
            )
        ).scalars()
    )
    if not stalled_plan_ids:
        return 0
    orders = list(
        (
            await db.execute(
                select(WorkOrder)
                .join(WorkPlan, WorkPlan.work_order_id == WorkOrder.id)
                .where(WorkPlan.id.in_(stalled_plan_ids))
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    recovered = 0
    for order in orders:
        if order.status != "ready":
            continue
        await transition_work_order(db, order, "running", actor=actor)
        recovered += 1
    return recovered


async def fail_attempt(
    db: AsyncSession,
    *,
    order: WorkOrder,
    step: WorkStep,
    attempt: WorkStepAttempt,
    error: dict[str, Any],
    retryable: bool,
    actor: str,
    checkpoint: dict[str, Any] | None = None,
) -> None:
    """checkpoint (Ф1.B, AGENT_AUTONOMY_ROADMAP.md): a capability that failed
    partway through real progress (e.g. an exploratory web_discover step that
    fetched 6 of 10 sources before timing out) can report that progress via
    PartialProgressError (tasks/work_orders.py) instead of losing it. Persisted
    here on the failed attempt; execute_claimed_step hands it to the next
    retry's input so the capability can resume instead of starting over.
    Every other failure path leaves this None — retries just restart clean,
    unchanged from before.

    Found while adding checkpoint (Ф1.B): this function never cleared
    step.lease_owner/lease_expires_at on failure — reclaim_expired_leases
    does (that's the *other* path into retry_wait, after a worker vanished),
    but the ordinary in-transaction failure path here didn't, so a step that
    fails and retries stayed unclaimable until its original claim_ready_step
    lease (default 120s) expired on its own, however short the computed
    backoff (as low as 5s) said the retry should wait — retries were
    silently rate-limited to whichever was longer. Cleared unconditionally
    below, matching reclaim_expired_leases, in both the retry and terminal
    branches (a step moving to "failed" has no more use for a lease either).
    """
    now = utcnow()
    attempt.status = "failed"
    attempt.error = error
    attempt.checkpoint = checkpoint
    attempt.finished_at = now
    attempt.heartbeat_at = now
    step.last_error = error
    step.lease_owner = None
    step.lease_expires_at = None
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
        max_replans = _max_replans_for(order)
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
        elif predicate_type == "coverage_report":
            # Ф1.D (AGENT_AUTONOMY_ROADMAP.md): honest-coverage for exploratory
            # WorkOrders, expressed as an ordinary criterion — no FSM/verdict
            # changes needed. Checks *shape* only (did the final step produce
            # a structured covered/partial/not_found account), not
            # exhaustiveness — an open-ended search can never promise "found
            # everything", only "here is an honest account of what I tried".
            # Whether not_found entries are genuinely justified (not
            # fabricated) is a separate "honest_not_found" criterion, judged
            # by the independent semantic verifier — see
            # exploratory_acceptance_criteria().
            report = output.get("coverage") if isinstance(output, dict) else None
            well_formed = isinstance(report, dict) and all(
                isinstance(report.get(key), list) for key in ("covered", "partial", "not_found")
            )
            nonempty = well_formed and any(
                report.get(key) for key in ("covered", "partial", "not_found")
            )
            verdict_ok = nonempty and not output.get("errors")
            if not well_formed:
                reason = "output.coverage is missing or not a {covered, partial, not_found} object"
            elif not nonempty:
                reason = "coverage report is present but all three lists are empty"
            else:
                reason = "well-formed coverage report with covered/partial/not_found lists"
            evidence_payload["coverage_counts"] = (
                {key: len(report.get(key) or []) for key in ("covered", "partial", "not_found")}
                if well_formed
                else None
            )

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
            if criterion.status != "pending":
                # Ф4-re (AGENT_AUTONOMY_ROADMAP.md): found live on the
                # persistence re-verification pilot — a semantic criterion
                # the independent verifier already rejected once (status
                # left "failed" by record_verifier_verdict) stayed "failed"
                # forever across every later replan attempt, because
                # nothing ever reset it. verify_semantic_criteria only
                # looks at criteria with status=="pending" — so after the
                # first rejection, the independent verifier never ran
                # again on any subsequent attempt, no matter how much
                # better the agent's new evidence was. The order kept
                # replanning (that part of the fix works) but could then
                # only ever cycle between "verifying" and
                # "independent_verification_required" — never able to
                # reach either "completed" or a budget-exhausted "blocked",
                # since nothing was left to judge it. Reset here, right
                # where this attempt determines the criterion still needs
                # independent judgment, so the fresh evidence from THIS
                # attempt gets a fresh verdict.
                criterion.status = "pending"
                criterion.verdict = None
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
        # Ф4-re (AGENT_AUTONOMY_ROADMAP.md): found live on the persistence
        # re-verification pilot — a required criterion genuinely failing
        # (e.g. coverage_report: the final step's own output wasn't even a
        # well-formed {covered,partial,not_found} shape) went straight to
        # "blocked" unconditionally, with zero regard for max_replans. Only
        # step-level EXECUTION failures (fail_attempt/reclaim_expired_leases)
        # ever consumed the bounded-replan budget the whole Ф4-до doработка
        # persistence fix raised to 30 for exploratory orders — a failure in
        # the agent's own synthesis never got a second try. order.blocker
        # (the specific reason set above) already flows into the next
        # plan's failure_context (plan_work_order reads order.blocker
        # directly) — the model already gets exactly this feedback on
        # replan, so this was purely a missing transition, not a missing
        # feedback channel.
        max_replans = _max_replans_for(order)
        target = "replanning" if order.plan_revision <= max_replans else "blocked"
        await transition_work_order(db, order, target, actor=actor)
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
        # Ф4-re (AGENT_AUTONOMY_ROADMAP.md): same bounded-replan treatment
        # as verify_nonempty_result's deterministic-failure branch — the
        # independent verifier rejecting a semantic criterion (e.g.
        # honest_not_found judging the agent's not_found entries fabricated
        # or unattempted) is exactly the same kind of recoverable failure
        # as a step erroring out, and deserves the same chance to try again
        # within budget. The order is typically "blocked" here already (the
        # earlier "independent_verification_required" hold from
        # verify_nonempty_result while this semantic check was pending) —
        # "blocked" -> "replanning" is a legal transition (see
        # WORK_TRANSITIONS). Guarded rather than unconditional in case a
        # caller reaches this from some other status this doesn't cover —
        # silently doing nothing there is safer than raising mid-verdict.
        max_replans = _max_replans_for(order)
        target = "replanning" if order.plan_revision <= max_replans else "blocked"
        if target in WORK_TRANSITIONS.get(order.status, frozenset()):
            await transition_work_order(db, order, target, actor=actor)
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
