"""Celery scheduler and executor for durable autonomous work orders."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import uuid
from datetime import timedelta
from typing import Any

import httpx

from app.domain.work_orders import (
    append_event,
    claim_ready_step,
    complete_attempt,
    enforce_budgets,
    fail_attempt,
    promote_ready_dependents,
    reclaim_expired_leases,
    record_verifier_verdict,
    transition_step,
    transition_work_order,
    utcnow,
    verify_nonempty_result,
)
from app.domain.work_planning import resolve_step_input, tool_call_digest
from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app


def _worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4()}"


class ApprovalRequiredError(RuntimeError):
    def __init__(self, capability: str, action: str, arguments: dict[str, Any]) -> None:
        super().__init__(f"Approval required for {capability}.{action}")
        self.capability = capability
        self.action = action
        self.arguments = arguments


def _action_digest(capability: str, action: str, arguments: dict[str, Any]) -> str:
    payload = {"capability": capability, "action": action, "arguments": arguments}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


async def _heartbeat_step(
    *,
    work_order_id: uuid.UUID,
    step_id: uuid.UUID,
    attempt_id: uuid.UUID,
    worker_id: str,
    stop: asyncio.Event,
    interval_seconds: int = 30,
    lease_seconds: int = 120,
    session_factory: Any | None = None,
) -> None:
    """Extend execution leases until the executor stops.

    Heartbeats run in independent short transactions, so a long external tool
    call never holds database locks and cannot be reclaimed as abandoned while
    its worker is still alive.
    """
    from app.db.models import WorkOrder, WorkStep, WorkStepAttempt
    from app.db.session import _get_session_factory
    from app.domain.work_orders import utcnow

    factory = session_factory or _get_session_factory()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass
        async with factory() as db:
            step = await db.get(WorkStep, step_id, with_for_update=True)
            attempt = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
            order = await db.get(WorkOrder, work_order_id, with_for_update=True)
            if (
                step is None
                or attempt is None
                or order is None
                or step.state != "running"
                or attempt.status != "running"
                or step.lease_owner != worker_id
            ):
                return
            now = utcnow()
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            step.lease_expires_at = lease_expires_at
            order.lease_expires_at = lease_expires_at
            attempt.heartbeat_at = now
            await db.commit()


async def _execute_agent_turn(input_data: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    from app.tasks.agent_cron import _run_headless_turn, run_headless_agent_turn

    prompt = str(input_data.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("agent_turn step requires a non-empty prompt")
    runner = _run_headless_turn if input_data.get("runner") == "cron" else run_headless_agent_turn
    ok, text, tokens_used = await asyncio.wait_for(runner(prompt), timeout=max(1, timeout_seconds))
    if not ok:
        raise RuntimeError(text or "Headless agent turn failed")
    return {"text": text, "executor": "agent_turn", "tokens_used": tokens_used}


async def _execute_capability(
    capability: str,
    action: str,
    input_data: dict[str, Any],
    timeout_seconds: int,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    from app.ai.agent_config import get_builtin_agent_config
    from app.ai.orchestrator import _agent_headers

    arguments = dict(input_data)
    approval = arguments.pop("approval", None)
    headers = _agent_headers()
    if idempotency_key:
        headers["X-Agent-Idempotency-Key"] = idempotency_key
    if isinstance(approval, dict):
        expected = _action_digest(capability, action, arguments)
        if approval.get("action_digest") == expected:
            headers["X-Agent-Approval"] = "granted"
    payload = {"action": action, **arguments}
    base_url = get_builtin_agent_config().backend_url.rstrip("/")
    async with httpx.AsyncClient(timeout=float(timeout_seconds)) as client:
        response = await client.post(
            f"{base_url}/api/agent/cap/{capability}",
            json=payload,
            headers=headers,
        )
    if response.status_code == 423:
        raise ApprovalRequiredError(capability, action, arguments)
    if response.status_code >= 500:
        raise ConnectionError(
            f"Capability {capability}.{action} failed with HTTP {response.status_code}"
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Capability {capability}.{action} rejected with HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )
    result = response.json() if response.content else {}
    if isinstance(result, dict) and (result.get("error") or result.get("error_code")):
        raise RuntimeError(str(result.get("error") or result.get("message") or result))
    summary = json.dumps(result, ensure_ascii=False, default=str)[:8000]
    return {"result": result, "result_summary": summary, "executor": "capability"}


async def _execute_step_kind(
    kind: str,
    input_data: dict[str, Any],
    timeout_seconds: int,
    *,
    capability: str | None,
    action: str | None,
    idempotency_key: str | None = None,
) -> dict:
    if kind == "agent_turn":
        return await _execute_agent_turn(input_data, timeout_seconds)
    if kind == "capability":
        if not capability or not action:
            raise ValueError("capability step requires capability and action")
        return await _execute_capability(
            capability, action, input_data, timeout_seconds, idempotency_key
        )
    raise ValueError(f"Unsupported durable work-step kind: {kind}")


async def verify_completed_step(step_id: uuid.UUID, *, session_factory: Any | None = None) -> bool:
    """Verify a succeeded step in a fresh transaction and execution context."""
    from app.db.models import WorkOrder, WorkStep
    from app.db.session import _get_session_factory

    factory = session_factory or _get_session_factory()
    async with factory() as db:
        step = await db.get(WorkStep, step_id, with_for_update=True)
        if step is None or step.state != "succeeded":
            return False
        order = await db.get(WorkOrder, step.work_order_id, with_for_update=True)
        if order is None:
            return False
        if order.status == "completed":
            return True
        if order.status != "running":
            return False
        if await promote_ready_dependents(
            db,
            order=order,
            plan_id=step.plan_id,
            actor="scheduler",
        ):
            await db.commit()
            return True
        passed = await verify_nonempty_result(db, order=order, step=step)
        await db.commit()
    completed = passed or await verify_semantic_criteria(
        step.work_order_id, session_factory=factory
    )
    if completed and session_factory is None:
        learn_work_order.apply_async(args=[str(step.work_order_id)], queue="scheduler")
    return completed


async def process_pending_work_learnings(limit: int = 20) -> int:
    """Retry durable learning jobs left pending or failed by transient services."""
    from sqlalchemy import and_, or_, select

    from app.db.models import WorkLearning
    from app.db.session import _get_session_factory
    from app.domain.work_learning import process_work_learning

    factory = _get_session_factory()
    async with factory() as db:
        order_ids = list(
            (
                await db.execute(
                    select(WorkLearning.work_order_id)
                    .where(
                        or_(
                            WorkLearning.status.in_(["pending", "failed"]),
                            and_(
                                WorkLearning.status == "processing",
                                WorkLearning.updated_at < utcnow() - timedelta(minutes=5),
                            ),
                        ),
                        WorkLearning.extraction_attempts < 5,
                    )
                    .order_by(WorkLearning.created_at)
                    .limit(limit)
                )
            ).scalars()
        )
    processed = 0
    for order_id in order_ids:
        if await process_work_learning(order_id):
            processed += 1
    return processed


async def verify_semantic_criteria(
    work_order_id: uuid.UUID, *, session_factory: Any | None = None
) -> bool:
    """Use a separate model call to judge unresolved semantic criteria."""
    from sqlalchemy import select

    from app.ai.model_resolver import get_reasoning_model
    from app.ai.ollama_client import generate_json
    from app.db.models import WorkAcceptanceCriterion, WorkOrder, WorkStep
    from app.db.session import _get_session_factory

    factory = session_factory or _get_session_factory()
    async with factory() as db:
        order = await db.get(WorkOrder, work_order_id)
        if order is None or order.status not in {"blocked", "verifying"}:
            return False
        criteria = list(
            (
                await db.execute(
                    select(WorkAcceptanceCriterion).where(
                        WorkAcceptanceCriterion.work_order_id == order.id,
                        WorkAcceptanceCriterion.required.is_(True),
                        WorkAcceptanceCriterion.status == "pending",
                    )
                )
            ).scalars()
        )
        if not criteria:
            return order.status == "completed"
        steps = list(
            (
                await db.execute(
                    select(WorkStep)
                    .where(WorkStep.work_order_id == order.id, WorkStep.state == "succeeded")
                    .order_by(WorkStep.finished_at)
                )
            ).scalars()
        )
        evidence = {
            "objective": order.objective,
            "description": order.description,
            "constraints": order.constraints,
            "criteria": [
                {"id": str(row.id), "key": row.criterion_key, "description": row.description, "predicate": row.predicate}
                for row in criteria
            ],
            "outputs": [{"step": row.step_key, "output": row.output} for row in steps],
        }
    model = get_reasoning_model(confidential=True)
    verdict = await generate_json(
        json.dumps(evidence, ensure_ascii=False, default=str),
        model=model.model,
        provider=model.provider,
        system="""You are an independent acceptance verifier. Judge only from supplied evidence.
Return JSON: {verdicts:[{criterion_id,ok,reason,checks:[string]}]}. Fail closed when
evidence is missing, contradictory, or does not demonstrate the objective. JSON only.""",
        temperature=0.0,
        max_tokens=4096,
        timeout_seconds=120,
    )
    by_id = {str(item.get("criterion_id")): item for item in verdict.get("verdicts", [])}
    completed = False
    async with factory() as db:
        order = await db.get(WorkOrder, work_order_id, with_for_update=True)
        if order is None:
            return False
        for criterion_id in [row.id for row in criteria]:
            criterion = await db.get(WorkAcceptanceCriterion, criterion_id, with_for_update=True)
            if criterion is None or criterion.status != "pending":
                continue
            item = by_id.get(str(criterion.id)) or {}
            completed = await record_verifier_verdict(
                db,
                order=order,
                criterion=criterion,
                ok=bool(item.get("ok", False)),
                reason=str(item.get("reason") or "Verifier returned no supported verdict"),
                evidence_payload={"checks": item.get("checks") or [], "model": model.model},
                actor="automatic-semantic-verifier",
            )
        await db.commit()
    return completed


async def execute_claimed_step(
    step_id: uuid.UUID,
    attempt_id: uuid.UUID,
    *,
    schedule_verification: bool = True,
    session_factory: Any | None = None,
) -> bool:
    """Execute an already-claimed step and settle it transactionally."""
    from app.db.models import WorkOrder, WorkStep, WorkStepAttempt, WorkToolCall
    from app.db.session import _get_session_factory

    factory = session_factory or _get_session_factory()
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        if step is None or attempt is None or attempt.status != "running":
            return False
        order = await db.get(WorkOrder, step.work_order_id)
        if order is None or order.status != "running":
            return False
        kind = step.kind
        try:
            input_data, resolved_from = await resolve_step_input(db, step)
        except Exception as exc:  # noqa: BLE001 - invalid persisted dataflow is terminal
            await fail_attempt(
                db,
                order=order,
                step=step,
                attempt=attempt,
                error={"code": "dataflow_resolution_error", "message": str(exc)},
                retryable=False,
                actor=str(attempt.worker_id),
            )
            await db.commit()
            return False
        timeout_seconds = step.timeout_seconds
        work_order_id = step.work_order_id
        capability = step.capability
        action = step.action
        step_idempotency_key = step.idempotency_key
        digest = tool_call_digest(kind, capability, action, input_data)
        call = WorkToolCall(
            work_order_id=work_order_id,
            step_id=step.id,
            attempt_id=attempt.id,
            call_no=1,
            executor=kind,
            capability=capability,
            action=action,
            arguments=input_data,
            resolved_from=resolved_from,
            risk_level=step.risk_level,
            status="prepared",
            action_digest=digest,
            idempotency_key=f"{step.idempotency_key}:attempt:{attempt.attempt_no}:call:1",
        )
        db.add(call)
        await db.flush()
        call_id = call.id
        await append_event(
            db,
            order.id,
            "tool_call.prepared",
            actor="executor",
            payload={"tool_call_id": str(call.id), "digest": digest, "capability": capability, "action": action},
        )
        await db.commit()

    worker = str(attempt.worker_id)
    async with factory() as db:
        call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
        if call_row is not None:
            call_row.status = "running"
            call_row.started_at = utcnow()
            await db.commit()
    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_step(
            work_order_id=work_order_id,
            step_id=step_id,
            attempt_id=attempt_id,
            worker_id=worker,
            stop=heartbeat_stop,
            session_factory=factory,
        )
    )
    try:
        output = await _execute_step_kind(
            kind,
            input_data,
            timeout_seconds,
            capability=capability,
            action=action,
            idempotency_key=step_idempotency_key,
        )
    except ApprovalRequiredError as exc:
        from app.db.models import Approval, ApprovalActionType

        digest = _action_digest(exc.capability, exc.action, exc.arguments)
        async with factory() as db:
            order = await db.get(WorkOrder, work_order_id, with_for_update=True)
            step_row = await db.get(WorkStep, step_id, with_for_update=True)
            attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
            call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
            if order and step_row and attempt_row and attempt_row.status == "running":
                approval = Approval(
                    action_type=ApprovalActionType.agent_tool_call,
                    entity_type="work_order",
                    entity_id=order.id,
                    requested_by=order.owner_key,
                    context={
                        "work_order_id": str(order.id),
                        "step_id": str(step_row.id),
                        "tool_name": exc.capability,
                        "action": exc.action,
                        "tool_args": exc.arguments,
                        "reason": "Capability gateway requires approval",
                        "action_digest": digest,
                    },
                )
                db.add(approval)
                await db.flush()
                attempt_row.status = "waiting_approval"
                attempt_row.finished_at = utcnow()
                if call_row is not None:
                    call_row.status = "waiting_approval"
                    call_row.finished_at = utcnow()
                await transition_step(
                    db,
                    step_row,
                    "waiting_approval",
                    actor="policy",
                    payload={"approval_id": str(approval.id), "action_digest": digest},
                )
                await transition_work_order(
                    db,
                    order,
                    "waiting_approval",
                    actor="policy",
                    payload={"approval_id": str(approval.id), "step_id": str(step_row.id)},
                )
                await db.commit()
        return False
    except (TimeoutError, ConnectionError) as exc:
        error = {
            "code": "transient_execution_error",
            "message": str(exc),
            "type": type(exc).__name__,
        }
        async with factory() as db:
            order = await db.get(WorkOrder, work_order_id, with_for_update=True)
            step_row = await db.get(WorkStep, step_id, with_for_update=True)
            attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
            call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
            if order and step_row and attempt_row and attempt_row.status == "running":
                await fail_attempt(
                    db,
                    order=order,
                    step=step_row,
                    attempt=attempt_row,
                    error=error,
                    retryable=True,
                    actor=worker,
                )
                if call_row is not None:
                    call_row.status = "failed"
                    call_row.error = error
                    call_row.finished_at = utcnow()
                await db.commit()
        return False
    except Exception as exc:  # noqa: BLE001 - persisted as a typed terminal attempt
        error = {"code": "execution_error", "message": str(exc), "type": type(exc).__name__}
        async with factory() as db:
            order = await db.get(WorkOrder, work_order_id, with_for_update=True)
            step_row = await db.get(WorkStep, step_id, with_for_update=True)
            attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
            call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
            if order and step_row and attempt_row and attempt_row.status == "running":
                await fail_attempt(
                    db,
                    order=order,
                    step=step_row,
                    attempt=attempt_row,
                    error=error,
                    retryable=False,
                    actor=worker,
                )
                if call_row is not None:
                    call_row.status = "failed"
                    call_row.error = error
                    call_row.finished_at = utcnow()
                await db.commit()
        return False
    finally:
        heartbeat_stop.set()
        await heartbeat

    async with factory() as db:
        order = await db.get(WorkOrder, work_order_id, with_for_update=True)
        step_row = await db.get(WorkStep, step_id, with_for_update=True)
        attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
        call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
        if not order or not step_row or not attempt_row or attempt_row.status != "running":
            return False
        await complete_attempt(
            db,
            order=order,
            step=step_row,
            attempt=attempt_row,
            output=output,
            actor=worker,
        )
        if call_row is not None:
            call_row.status = "succeeded"
            call_row.output = output
            call_row.finished_at = utcnow()
        await db.commit()
    if schedule_verification:
        verify_work_step.apply_async(args=[str(step_id)], queue="scheduler")
    else:
        await verify_completed_step(step_id, session_factory=factory)
    return True


async def execute_work_order_now(
    work_order_id: uuid.UUID, *, session_factory: Any | None = None
) -> bool:
    """Claim and execute the next ready step for one order.

    Used by the compatibility API. Background dispatch uses the same claim and
    execution functions, so there is no second policy/state path.
    """
    from app.db.session import _get_session_factory

    worker = _worker_id()
    factory = session_factory or _get_session_factory()
    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id=worker, work_order_id=work_order_id)
        if claimed is None:
            return False
        _order, step, attempt = claimed
        step_id = step.id
        attempt_id = attempt.id
        await db.commit()
    return await execute_claimed_step(
        step_id,
        attempt_id,
        schedule_verification=False,
        session_factory=factory,
    )


async def _dispatch_ready_work(limit: int = 10) -> int:
    from sqlalchemy import select

    from app.db.models import WorkOrder, WorkStep
    from app.db.session import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        planning_ids = list(
            (
                await db.execute(
                    select(WorkOrder.id)
                    .where(WorkOrder.status.in_(["received", "planning", "replanning"]))
                    .order_by(WorkOrder.priority.desc(), WorkOrder.created_at)
                    .limit(10)
                )
            ).scalars()
        )
    for order_id in planning_ids:
        plan_work_order_task.apply_async(args=[str(order_id)], queue="scheduler")
    async with factory() as db:
        await reclaim_expired_leases(db)
        await enforce_budgets(db)
        pending_verification = list(
            (
                await db.execute(
                    select(WorkStep.id)
                    .join(WorkOrder, WorkOrder.id == WorkStep.work_order_id)
                    .where(WorkStep.state == "succeeded", WorkOrder.status == "running")
                    .limit(100)
                )
            ).scalars()
        )
        await db.commit()

    for step_id in pending_verification:
        verify_work_step.apply_async(args=[str(step_id)], queue="scheduler")

    claimed_ids: list[tuple[str, str]] = []
    for _ in range(max(1, min(limit, 100))):
        worker = _worker_id()
        async with factory() as db:
            claimed = await claim_ready_step(db, worker_id=worker)
            if claimed is None:
                break
            _order, step, attempt = claimed
            claimed_ids.append((str(step.id), str(attempt.id)))
            await db.commit()
    for step_id, attempt_id in claimed_ids:
        execute_work_step.apply_async(args=[step_id, attempt_id], queue="scheduler")
    return len(claimed_ids)


async def _plan_order(work_order_id: uuid.UUID) -> bool:
    from app.db.models import WorkOrder
    from app.db.session import _get_session_factory
    from app.domain.work_planning import plan_work_order

    factory = _get_session_factory()
    async with factory() as db:
        order = await db.get(WorkOrder, work_order_id, with_for_update=True)
        if order is None or order.status not in {"received", "planning", "replanning"}:
            return False
        await plan_work_order(db, order, use_model=True)
        order.blocker = None
        await db.commit()
        return True


@celery_app.task(name="work.plan_order", queue="scheduler", max_retries=0, ignore_result=True)
def plan_work_order_task(work_order_id: str) -> None:
    run_async(_plan_order(uuid.UUID(work_order_id)))


@celery_app.task(
    name="work.dispatch_ready",
    queue="scheduler",
    max_retries=0,
    ignore_result=True,
)
def dispatch_ready_work() -> None:
    run_async(_dispatch_ready_work())


@celery_app.task(
    name="work.execute_step",
    queue="scheduler",
    max_retries=0,
    ignore_result=True,
    soft_time_limit=650,
    time_limit=700,
)
def execute_work_step(step_id: str, attempt_id: str) -> None:
    run_async(execute_claimed_step(uuid.UUID(step_id), uuid.UUID(attempt_id)))


@celery_app.task(
    name="work.verify_step",
    queue="scheduler",
    max_retries=0,
    ignore_result=True,
    soft_time_limit=120,
    time_limit=150,
)
def verify_work_step(step_id: str) -> None:
    run_async(verify_completed_step(uuid.UUID(step_id)))


@celery_app.task(
    name="work.learn_order",
    queue="scheduler",
    max_retries=0,
    ignore_result=True,
    soft_time_limit=120,
    time_limit=150,
)
def learn_work_order(work_order_id: str) -> None:
    from app.domain.work_learning import process_work_learning

    run_async(process_work_learning(uuid.UUID(work_order_id)))


@celery_app.task(
    name="work.learn_pending",
    queue="scheduler",
    max_retries=0,
    ignore_result=True,
)
def learn_pending_work_orders() -> None:
    run_async(process_pending_work_learnings())


@celery_app.task(
    name="work.expire_memory",
    queue="scheduler",
    max_retries=0,
    ignore_result=True,
)
def expire_work_memory() -> None:
    from app.domain.work_learning import expire_stale_work_memory

    run_async(expire_stale_work_memory())
