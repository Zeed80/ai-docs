"""Celery scheduler and executor for durable autonomous work orders."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from app.ai.chat_checkpoint import ChatNonterminalToolResult, ChatWaitingApprovalToolResult
from app.ai.work_budget_context import BudgetExecutionStopped, WorkBudgetContext
from app.domain.work_orders import (
    WorkStateError,
    append_event,
    attempt_owns_lease,
    claim_ready_step,
    complete_attempt,
    enforce_budgets,
    enter_waiting_for_children,
    fail_attempt,
    promote_ready_dependents,
    promote_waiting_parents,
    reclaim_expired_leases,
    record_verifier_verdict,
    recorded_recipient_action_digest,
    stop_attempt_for_nonterminal_tool_result,
    transition_step,
    transition_work_order,
    utcnow,
    validate_recorded_recipient_binding,
    verify_nonempty_result,
)
from app.domain.work_planning import resolve_step_input, tool_call_digest
from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


def _worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4()}"


class ApprovalRequiredError(RuntimeError):
    def __init__(
        self,
        capability: str,
        action: str,
        arguments: dict[str, Any],
        *,
        tool_result: dict[str, Any] | None = None,
        budget_stop: BudgetExecutionStopped | None = None,
        recipient_output: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"Approval required for {capability}.{action}")
        self.capability = capability
        self.action = action
        self.arguments = arguments
        self.tool_result = tool_result
        self.budget_stop = budget_stop
        self.recipient_output = recipient_output


class PartialProgressError(RuntimeError):
    """Ф1.B: a capability failed partway through but made real progress worth
    keeping — e.g. an exploratory discovery step that fetched 6 of 10 sources
    before timing out. Raised instead of a plain RuntimeError/ConnectionError
    when the capability's error response includes a ``checkpoint`` object;
    execute_claimed_step persists it (WorkStepAttempt.checkpoint) and merges
    it into the next retry's input as ``_resume_checkpoint`` so the capability
    can resume rather than redo already-done work. Always retryable — that's
    the point of reporting a checkpoint at all.
    """

    def __init__(
        self,
        message: str,
        *,
        checkpoint: dict[str, Any],
        budget_stop: BudgetExecutionStopped | None = None,
        recipient_output: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.checkpoint = checkpoint
        self.budget_stop = budget_stop
        self.recipient_output = recipient_output


class NonterminalToolResultError(RuntimeError):
    """A validated v1 ToolResult that must stop, not retry, a WorkOrder."""

    def __init__(
        self,
        result: dict[str, Any],
        *,
        budget_stop: BudgetExecutionStopped | None = None,
        recipient_output: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(str(result.get("status")))
        self.result = result
        self.budget_stop = budget_stop
        self.recipient_output = recipient_output


class CapabilityBudgetExecutionStopped(BudgetExecutionStopped):
    """Budget stop after a capability transport crossed its dispatch boundary.

    Recipient evidence is deliberately an attribute rather than part of
    ``details``: callers persist it in the attempt/tool-call output, while the
    blocker and logs keep arbitrary response bodies out of error payloads.
    """

    def __init__(
        self,
        stop: BudgetExecutionStopped,
        *,
        recipient_output: dict[str, Any],
    ) -> None:
        super().__init__(stop.code, stop.message, details=dict(stop.details))
        self.recipient_output = recipient_output


def _budget_stop(context: Any) -> BudgetExecutionStopped | None:
    try:
        context.raise_if_stopped()
    except BudgetExecutionStopped as exc:
        return exc
    return None


def _capability_response_output(response: httpx.Response) -> dict[str, Any]:
    # ``recipient_confirmed`` means only that an HTTP response was received.
    # It never asserts that the requested business effect happened.
    output: dict[str, Any] = {
        "executor": "capability",
        "http_status": response.status_code,
        "recipient_confirmed": True,
    }
    if not response.content:
        output["result"] = {}
        return output
    try:
        output["result"] = response.json()
    except Exception:
        output["response_text"] = response.text[:8000]
    return output


def _action_digest(capability: str, action: str, arguments: dict[str, Any]) -> str:
    return recorded_recipient_action_digest(capability, action, arguments)


async def _notify_computer_use_needs_grant(
    db: Any, *, order: Any, capability_action: str, reason: str
) -> None:
    """Ф2.B (AGENT_AUTONOMY_ROADMAP.md): notify-before-scope for computer_use,
    not an approval — deciding the digest-Approval created alongside this
    (see the caller) never creates a ComputerUseGrant, which only a manager
    can (POST /work-orders/{id}/computer-grants). Best-effort: a notification
    failure must never break the step-failure path that's already in
    progress when this is called.
    """
    from app.db.models import NotificationType
    from app.services.notifications import create_notification

    try:
        await create_notification(
            db,
            user_sub=order.owner_key,
            type=NotificationType.system,
            title="Нужен доступ в интернет для поручения",
            body=(
                f"Поручение «{order.objective[:200]}» пытается выполнить "
                f"computer_use.{capability_action}, но для него нет активного "
                "разрешения (ComputerUseGrant). Создать его может только "
                f"руководитель: POST /work-orders/{order.id}/computer-grants. "
                f"({reason[:200]})"
            ),
            entity_type="work_order",
            entity_id=order.id,
            action_url=f"/work-orders/{order.id}",
            source_task="workorder.needs_computer_use_grant",
        )
    except Exception as exc:  # noqa: BLE001 - never break step-failure handling
        logger.warning(
            "computer_use_grant_notification_failed", work_order_id=str(order.id), error=str(exc)
        )


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
            # Ф4-re (AGENT_AUTONOMY_ROADMAP.md): found live on the
            # persistence re-verification pilot — a real Postgres deadlock
            # among 4 concurrently executing sibling steps of the same
            # plan, all raising DeadlockDetectedError. step/attempt rows
            # are per-step-unique (no cross-sibling contention), but
            # work_orders is the ONE row every concurrently-running
            # sibling's heartbeat (every 30s, one per active step) plus
            # its own execute_claimed_step/claim_ready_step/complete_
            # attempt/fail_attempt flow all reach for — with_for_update
            # here reserved an exclusive lock on it well before the write
            # that actually needed one, for no correctness benefit this
            # heartbeat requires (order.lease_expires_at is an informational
            # timestamp, not a value anything depends on being strictly
            # serialized against a concurrent sibling's own heartbeat).
            # Dropped here specifically — the update below still commits
            # safely under Postgres's normal implicit per-statement lock,
            # held only for the instant of the UPDATE instead of reserved
            # for the whole transaction, which is what was creating the
            # circular-wait window across concurrent siblings.
            order = await db.get(WorkOrder, work_order_id)
            if (
                step is None
                or attempt is None
                or order is None
                or step.state != "running"
                or attempt.status != "running"
                or step.lease_owner != worker_id
                or not attempt_owns_lease(step, attempt)
            ):
                return
            now = utcnow()
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            step.lease_expires_at = lease_expires_at
            order.lease_expires_at = lease_expires_at
            attempt.heartbeat_at = now
            await db.commit()


async def _execute_capability(
    capability: str,
    action: str,
    input_data: dict[str, Any],
    timeout_seconds: int,
    idempotency_key: str | None = None,
    budget_context: Any | None = None,
) -> dict[str, Any]:
    from app.ai.agent_config import get_builtin_agent_config
    from app.ai.orchestrator import _agent_headers

    arguments = dict(input_data)
    if capability == "workspace" and action == "sql_table":
        # The generic capability proxy has no recipient-bound handoff. Its
        # WorkToolCall was already committed by execute_claimed_step, preserving
        # historic evidence, but it must not cross the SQL recipient's LLM edge.
        raise BudgetExecutionStopped(
            "workspace_sql_table_recipient_handoff_required",
            "Generic workspace.sql_table execution is disabled until it can issue "
            "the verified direct HTTP recipient handoff",
        )
    approval = arguments.pop("approval", None)
    headers = _agent_headers()
    if idempotency_key:
        headers["X-Agent-Idempotency-Key"] = idempotency_key
    if isinstance(approval, dict):
        expected = _action_digest(capability, action, arguments)
        if approval.get("action_digest") == expected:
            headers["X-Agent-Approval"] = "granted"
            from app.ai.agent_loop import capability_args_digest

            headers["X-Agent-Approval-Digest"] = capability_args_digest(
                {"action": action, **arguments}
            )
    payload = {"action": action, **arguments}
    base_url = get_builtin_agent_config().backend_url.rstrip("/")
    url = f"{base_url}/api/agent/cap/{capability}"
    operation_key: str | None = None
    settlement_ok = True
    response: httpx.Response | None = None
    try:
        async with httpx.AsyncClient(timeout=float(timeout_seconds)) as client:
            if budget_context is not None:
                operation_key = await budget_context.prepare_tool_attempt(
                    method="POST",
                    url=url,
                    request={
                        "body": payload,
                        "approval_granted": headers.get("X-Agent-Approval") == "granted",
                        "idempotency_key": idempotency_key,
                    },
                )
            try:
                response = await client.post(url, json=payload, headers=headers)
            except BaseException as exc:
                if budget_context is not None and operation_key is not None:
                    settlement_ok = await budget_context.charge_tool_attempt(
                        operation_key,
                        recipient_outcome="unconfirmed",
                    )
                    if not settlement_ok:
                        stop = _budget_stop(budget_context)
                        if stop is not None:
                            raise CapabilityBudgetExecutionStopped(
                                stop,
                                recipient_output={
                                    "executor": "capability",
                                    "recipient_confirmed": False,
                                    "transport_error_type": type(exc).__name__,
                                },
                            ) from exc
                raise
            else:
                if budget_context is not None and operation_key is not None:
                    settlement_ok = await budget_context.charge_tool_attempt(
                        operation_key,
                        recipient_outcome="responded",
                    )
    except BaseException:
        if response is None or settlement_ok:
            raise
        # The response is stronger evidence than a later client-cleanup
        # failure. Continue into normal response classification so validated
        # nonterminal/approval lifecycle is not flattened into a budget error.
    assert response is not None
    recipient_output = _capability_response_output(response)
    pending_budget_stop = None if settlement_ok else _budget_stop(budget_context)
    from app.ai.tool_result import normalize_tool_result, result_failed

    if response.status_code == 423:
        tool_result = None
        candidate = recipient_output.get("result")
        if isinstance(candidate, dict) and "version" in candidate:
            try:
                normalized = normalize_tool_result(candidate)
            except Exception:
                pass
            else:
                if normalized.status == "waiting_approval":
                    tool_result = normalized.model_dump(mode="json")
        raise ApprovalRequiredError(
            capability,
            action,
            arguments,
            tool_result=tool_result if isinstance(tool_result, dict) else None,
            budget_stop=pending_budget_stop,
            recipient_output=recipient_output,
        )
    if response.status_code >= 400:
        # A capability that made real progress before failing may report it
        # as {"error": ..., "checkpoint": {...}} in its response body — a
        # convention, not a contract every capability has to implement;
        # absent or malformed, this behaves exactly as before (plain
        # RuntimeError/ConnectionError, retry starts clean).
        checkpoint = None
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("checkpoint"), dict):
                checkpoint = body["checkpoint"]
        except Exception:
            pass
        if response.status_code >= 500:
            if checkpoint:
                raise PartialProgressError(
                    f"Capability {capability}.{action} failed with HTTP {response.status_code}",
                    checkpoint=checkpoint,
                    budget_stop=pending_budget_stop,
                    recipient_output=recipient_output,
                )
            if pending_budget_stop is not None:
                raise CapabilityBudgetExecutionStopped(
                    pending_budget_stop,
                    recipient_output=recipient_output,
                )
            raise ConnectionError(
                f"Capability {capability}.{action} failed with HTTP {response.status_code}"
            )
        message = (
            f"Capability {capability}.{action} rejected with HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )
        if checkpoint:
            raise PartialProgressError(
                message,
                checkpoint=checkpoint,
                budget_stop=pending_budget_stop,
                recipient_output=recipient_output,
            )
        if pending_budget_stop is not None:
            raise CapabilityBudgetExecutionStopped(
                pending_budget_stop,
                recipient_output=recipient_output,
            )
        raise RuntimeError(message)
    if "result" in recipient_output:
        result = recipient_output["result"]
    elif pending_budget_stop is not None:
        raise CapabilityBudgetExecutionStopped(
            pending_budget_stop,
            recipient_output=recipient_output,
        )
    else:
        result = response.json()
    # Legacy maps keep their historical behaviour: E05 intentionally leaves
    # deferred operations raw.  Only a validated v1 envelope has enough
    # lifecycle meaning to stop the durable consumer before it retries,
    # verifies, releases dependents, or replans.
    if isinstance(result, dict) and "version" in result:
        normalized = normalize_tool_result(result)
        if normalized.status == "waiting_approval":
            raise ApprovalRequiredError(
                capability,
                action,
                arguments,
                tool_result=normalized.model_dump(mode="json"),
                budget_stop=pending_budget_stop,
                recipient_output=recipient_output,
            )
        if normalized.status in {"partial", "outcome_unknown"}:
            raise NonterminalToolResultError(
                normalized.model_dump(mode="json"),
                budget_stop=pending_budget_stop,
                recipient_output=recipient_output,
            )
        if normalized.status == "failed":
            result = normalized.model_dump(mode="json")
            if pending_budget_stop is not None:
                raise CapabilityBudgetExecutionStopped(
                    pending_budget_stop,
                    recipient_output=recipient_output,
                )
            message = str(result.get("error") or result.get("message") or result)
            # A versioned failure is not the legacy checkpoint convention.
            # Its retryability cannot broaden WorkOrder's conservative retry
            # policy; explicit recipient retry support needs its own contract.
            raise RuntimeError(message)

    if result_failed(result):
        recipient_output["result"] = result
        message = str(result.get("error") or result.get("message") or result)
        checkpoint = result.get("checkpoint")
        if isinstance(checkpoint, dict):
            raise PartialProgressError(
                message,
                checkpoint=checkpoint,
                budget_stop=pending_budget_stop,
                recipient_output=recipient_output,
            )
        if pending_budget_stop is not None:
            raise CapabilityBudgetExecutionStopped(
                pending_budget_stop,
                recipient_output=recipient_output,
            )
        raise RuntimeError(message)
    summary = json.dumps(result, ensure_ascii=False, default=str)[:8000]
    output = {"result": result, "result_summary": summary, "executor": "capability"}
    if pending_budget_stop is not None:
        recipient_output["result_summary"] = summary
        raise CapabilityBudgetExecutionStopped(
            pending_budget_stop,
            recipient_output=recipient_output,
        )
    return output


async def _execute_step_kind(
    kind: str,
    input_data: dict[str, Any],
    timeout_seconds: int,
    *,
    capability: str | None,
    action: str | None,
    idempotency_key: str | None = None,
    work_order_id: uuid.UUID | None = None,
    budget_context: Any | None = None,
) -> dict:
    if kind == "agent_turn":
        # Production durable chat is dispatched before this helper. Any other
        # agent_turn used to instantiate a bare AgentSession without the
        # WorkOrder ledger or recipient checkpoint. Keep direct/internal calls
        # fail-closed as defense in depth.
        raise RuntimeError("headless_agent_turn_requires_durable_intake")
    if kind == "capability":
        if not capability or not action:
            raise ValueError("capability step requires capability and action")
        return await _execute_capability(
            capability,
            action,
            input_data,
            timeout_seconds,
            idempotency_key,
            budget_context,
        )
    if kind == "decompose":
        if work_order_id is None:
            raise ValueError("decompose step requires work_order_id")
        return await _execute_decompose(work_order_id, input_data)
    raise ValueError(f"Unsupported durable work-step kind: {kind}")


def _split_child_budgets(
    parent_budgets: dict[str, Any], child_count: int, child_override: dict[str, Any] | None
) -> dict[str, Any]:
    """Б11/Б15: a child never inherits the parent's full token_budget unsplit
    — summed across N children that would let the group spend N times what
    the parent was allowed. An explicit per-child override (PlannedChildSpec.
    budgets) always wins; otherwise the parent's token_budget (if any) is
    split evenly. max_replans is NOT divided — each child gets its own full
    replanning allowance, that budget is about retry depth, not spend."""
    if child_override:
        return dict(child_override)
    budgets: dict[str, Any] = {}
    parent_token_budget = parent_budgets.get("token_budget")
    if parent_token_budget is not None and child_count > 0:
        budgets["token_budget"] = max(1, int(parent_token_budget) // child_count)
    return budgets


async def _execute_decompose(work_order_id: uuid.UUID, input_data: dict[str, Any]) -> dict:
    """Б11: create one child WorkOrder per input.children entry.

    Runs in its own transaction (unlike agent_turn/capability steps, this one
    needs direct DB access to create rows) — commits before returning so the
    children exist and are dispatchable the moment this step is observed as
    succeeded. The parent order's own transition to "waiting_external" (not
    "completed" — a decompose step succeeding is not the parent's objective
    being done) happens separately in verify_completed_step, which is the
    existing hook for "what happens after the last step of a plan succeeds".
    """
    from sqlalchemy import select

    from app.db.models import WorkOrder
    from app.db.session import _get_session_factory
    from app.domain.work_budget_ledger import bind_child_to_parent_ledger
    from app.domain.work_orders import create_work_order

    children_spec = list(input_data.get("children") or [])
    if not children_spec:
        raise ValueError("decompose step requires a non-empty children list")
    # Children are committed before this step settles; a crash in between
    # retries the step. The digest makes that retry return the same children
    # instead of spawning a second set.
    spec_digest = hashlib.sha256(
        json.dumps(children_spec, sort_keys=True, ensure_ascii=True, default=str).encode()
    ).hexdigest()

    factory = _get_session_factory()
    child_ids: list[str] = []
    async with factory() as db:
        parent = await db.get(WorkOrder, work_order_id, with_for_update=True)
        if parent is None:
            raise ValueError(f"parent work order {work_order_id} not found")
        existing = [
            row
            for row in await db.scalars(select(WorkOrder).where(WorkOrder.parent_id == parent.id))
            if (row.metadata_ or {}).get("decompose_spec_digest") == spec_digest
        ]
        if existing:
            return {
                "text": f"Создано дочерних поручений: {len(existing)}",
                "executor": "decompose",
                "child_order_ids": [str(row.id) for row in existing],
            }
        parent_budgets = dict(parent.budgets or {})
        for spec in children_spec:
            objective = str(spec.get("objective") or "").strip()
            if not objective:
                continue
            child_budgets = _split_child_budgets(
                parent_budgets, len(children_spec), spec.get("budgets")
            )
            child = await create_work_order(
                db,
                owner_key=parent.owner_key,
                objective=objective,
                description=spec.get("description"),
                source="decompose",
                # A child never outranks the parent that spawned it — same
                # ceiling, never higher, for both priority and risk gating.
                priority=parent.priority,
                risk_level=parent.risk_level,
                budgets=child_budgets,
                # Ф4: inherit constraints (notably mode="exploratory") — a
                # child decomposed from an exploratory objective is itself
                # still exploratory and should get the same planner guidance.
                constraints=dict(parent.constraints or {}),
                parent_id=parent.id,
                metadata={"decompose_spec_digest": spec_digest},
            )
            # E21: the child spends from the parent's shared ledger, and it is
            # left "received" for the durable capability planner. It used to
            # get a single agent_turn step, which E21.2b5 retired for headless
            # work, so no decomposed child could ever run.
            await bind_child_to_parent_ledger(db, parent, child)
            child_ids.append(str(child.id))
        await db.commit()

    return {
        "text": f"Создано дочерних поручений: {len(child_ids)}",
        "executor": "decompose",
        "child_order_ids": child_ids,
    }


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
        if order.status == "ready":
            # Ф4 (AGENT_AUTONOMY_ROADMAP.md): found live on the Ф4 pilot — a
            # succeeded step whose order ends up "ready" instead of
            # "running" (e.g. it was the last claimable step, so nothing
            # was left to keep the order in "running" until verification
            # ran) used to be stuck forever here: this function required
            # exactly "running", and nothing elsewhere ever transitions a
            # "ready" order back to "running" on its own — claim_ready_step
            # only does that when there's a ready/retry_wait step left to
            # claim, and there isn't one once everything has succeeded.
            # domain.work_orders.unstick_ready_orders_with_stalled_active_plan
            # (called from _dispatch_ready_work, before this function is ever
            # queued) is the primary fix for that starvation; this is
            # defense-in-depth for any other caller that reaches
            # verify_completed_step directly. "ready"->"running" is itself a
            # legal transition (the same one claim_ready_step performs when
            # picking up work), so recover it here before falling through to
            # the normal path below, whose first step
            # (verify_nonempty_result's own transition to "verifying") is
            # only a legal move from "running"/"waiting_external", not from
            # "ready".
            await transition_work_order(db, order, "running", actor="scheduler")
        elif order.status != "running":
            return False
        if await promote_ready_dependents(
            db,
            order=order,
            plan_id=step.plan_id,
            actor="scheduler",
        ):
            await db.commit()
            return True
        # Б11: a succeeded decompose step's "result" is the children it
        # spawned, not a business result of its own — the parent order isn't
        # done, it's waiting on them. Enters "waiting_external" instead of
        # the normal verify_nonempty_result path; promote_waiting_parents
        # (called from _dispatch_ready_work, same periodic-housekeeping
        # pattern as reclaim_expired_leases/enforce_budgets) completes the
        # parent once every child reaches a terminal state.
        if (
            step.kind == "decompose"
            and isinstance(step.output, dict)
            and step.output.get("child_order_ids")
        ):
            await enter_waiting_for_children(db, order=order, actor="scheduler")
            await db.commit()
            return False
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


@dataclass(frozen=True)
class _VerifierSnapshot:
    work_order_id: uuid.UUID
    owner_key: str
    plan_id: uuid.UUID
    plan_revision: int
    criterion_ids: tuple[uuid.UUID, ...]
    evidence: dict[str, Any]
    digest: str


async def _read_verifier_snapshot(
    db: Any, work_order_id: uuid.UUID, *, lock_order: bool
) -> _VerifierSnapshot | None:
    """Read the complete server-owned verifier input under the order lock."""
    from app.db.models import WorkAcceptanceCriterion, WorkOrder, WorkPlan, WorkStep

    order = await db.get(WorkOrder, work_order_id, with_for_update=lock_order)
    if order is None or order.status not in {"blocked", "verifying"}:
        return None
    plans = list(
        (
            await db.execute(
                select(WorkPlan)
                .where(
                    WorkPlan.work_order_id == order.id,
                    WorkPlan.revision == order.plan_revision,
                    WorkPlan.status == "active",
                )
                .order_by(WorkPlan.id)
            )
        ).scalars()
    )
    if len(plans) != 1:
        return None
    plan = plans[0]
    criteria = list(
        (
            await db.execute(
                select(WorkAcceptanceCriterion)
                .where(
                    WorkAcceptanceCriterion.work_order_id == order.id,
                    WorkAcceptanceCriterion.required.is_(True),
                    WorkAcceptanceCriterion.status == "pending",
                )
                .order_by(WorkAcceptanceCriterion.criterion_key, WorkAcceptanceCriterion.id)
            )
        ).scalars()
    )
    if not criteria:
        return None
    criterion_keys = [row.criterion_key for row in criteria]
    if order.status == "blocked":
        blocker = order.blocker or {}
        if blocker.get("code") != "independent_verification_required" or sorted(
            blocker.get("criteria") or []
        ) != sorted(criterion_keys):
            return None
    steps = list(
        (
            await db.execute(
                select(WorkStep)
                .where(
                    WorkStep.work_order_id == order.id,
                    WorkStep.plan_id == plan.id,
                    WorkStep.state == "succeeded",
                )
                .order_by(WorkStep.finished_at, WorkStep.id)
            )
        ).scalars()
    )
    evidence = {
        "objective": order.objective,
        "description": order.description,
        "constraints": order.constraints,
        "criteria": [
            {
                "id": str(row.id),
                "key": row.criterion_key,
                "description": row.description,
                "predicate": row.predicate,
            }
            for row in criteria
        ],
        "outputs": [{"step": row.step_key, "output": row.output} for row in steps],
    }
    identity = {
        "work_order_id": str(order.id),
        "owner_key": order.owner_key,
        "status": order.status,
        "blocker": order.blocker,
        "plan_id": str(plan.id),
        "plan_revision": order.plan_revision,
        "evidence": evidence,
    }
    digest = hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode()
    ).hexdigest()
    return _VerifierSnapshot(
        work_order_id=order.id,
        owner_key=order.owner_key,
        plan_id=plan.id,
        plan_revision=order.plan_revision,
        criterion_ids=tuple(row.id for row in criteria),
        evidence=evidence,
        digest=digest,
    )


async def _verifier_snapshot_is_current(factory: Any, snapshot: _VerifierSnapshot) -> bool:
    async with factory() as db:
        current = await _read_verifier_snapshot(db, snapshot.work_order_id, lock_order=True)
        return current is not None and current.digest == snapshot.digest


async def _persist_verifier_stop(
    factory: Any, snapshot: _VerifierSnapshot, stop: BudgetExecutionStopped
) -> bool:
    if stop.code == "verification_execution_already_started":
        # A concurrent loser must not mutate the winner's authoritative state.
        return False
    async with factory() as db:
        current = await _read_verifier_snapshot(db, snapshot.work_order_id, lock_order=True)
        if current is None or current.digest != snapshot.digest:
            return False
        from app.db.models import WorkOrder

        order = await db.get(WorkOrder, snapshot.work_order_id)
        if order is None:
            return False
        order.blocker = stop.as_error()
        if order.status == "verifying":
            await transition_work_order(
                db,
                order,
                "blocked",
                actor="automatic-semantic-verifier",
                payload={"reason": stop.code},
            )
        await append_event(
            db,
            order.id,
            "verification.budget_stopped",
            actor="automatic-semantic-verifier",
            payload={"code": stop.code, "plan_revision": snapshot.plan_revision},
        )
        await db.commit()
        return True


async def verify_semantic_criteria(
    work_order_id: uuid.UUID, *, session_factory: Any | None = None
) -> bool:
    """Use a separate model call to judge unresolved semantic criteria."""
    from app.ai.model_resolver import get_reasoning_model
    from app.ai.ollama_client import generate_json
    from app.ai.work_budget_context import DetachedVerifierBudgetContext
    from app.db.models import WorkAcceptanceCriterion, WorkOrder
    from app.db.session import _get_session_factory

    factory = session_factory or _get_session_factory()
    async with factory() as db:
        snapshot = await _read_verifier_snapshot(db, work_order_id, lock_order=True)
    if snapshot is None:
        return False
    model = get_reasoning_model(confidential=True)
    scope = f"{snapshot.work_order_id.hex}:r{snapshot.plan_revision}:{snapshot.digest[:24]}"
    budget_context = DetachedVerifierBudgetContext(
        work_order_id=snapshot.work_order_id,
        owner_key=snapshot.owner_key,
        snapshot_digest=snapshot.digest,
        operation_scope=scope,
        session_factory=factory,
        snapshot_is_current=lambda: _verifier_snapshot_is_current(factory, snapshot),
    )
    try:
        verdict = await generate_json(
            json.dumps(snapshot.evidence, ensure_ascii=False, default=str),
            model=model.model,
            provider=model.provider,
            system="""You are an independent acceptance verifier. Judge only from supplied evidence.
Return JSON: {verdicts:[{criterion_id,ok,reason,checks:[string]}]}. Fail closed when
evidence is missing, contradictory, or does not demonstrate the objective. JSON only.""",
            temperature=0.0,
            max_tokens=4096,
            timeout_seconds=120,
            budget_context=budget_context,
        )
    except BudgetExecutionStopped as stop:
        await _persist_verifier_stop(factory, snapshot, stop)
        return False
    except Exception as exc:
        stop = BudgetExecutionStopped(
            "verification_provider_failed",
            "Semantic verifier provider failed without a supported verdict",
            details={"provider_error": str(exc)[:1000]},
        )
        await _persist_verifier_stop(factory, snapshot, stop)
        return False
    from pydantic import ValidationError

    from app.ai.tool_result import VerifierResponse

    try:
        checked = VerifierResponse.model_validate(verdict)
        by_id = {item.criterion_id: item.model_dump() for item in checked.verdicts}
    except ValidationError:
        by_id = {}
    completed = False
    async with factory() as db:
        current = await _read_verifier_snapshot(db, work_order_id, lock_order=True)
        if current is None or current.digest != snapshot.digest:
            return False
        order = await db.get(WorkOrder, work_order_id)
        if order is None:
            return False
        for criterion_id in snapshot.criterion_ids:
            criterion = await db.get(WorkAcceptanceCriterion, criterion_id, with_for_update=True)
            if criterion is None or criterion.status != "pending":
                continue
            item = by_id.get(str(criterion.id)) or {}
            completed = await record_verifier_verdict(
                db,
                order=order,
                criterion=criterion,
                ok=item.get("ok") is True,
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
        root_order_id = await db.scalar(
            select(WorkStep.work_order_id).where(WorkStep.id == step_id)
        )
        if root_order_id is None:
            return False
        # Match cancellation and terminal settlement: WorkOrder is the root
        # lock. Do not lock WorkStep here; claim_ready_step intentionally has
        # a different step-first path and this preparation must not introduce
        # a new order<->step inversion.
        order = await db.get(WorkOrder, root_order_id, with_for_update=True)
        step = await db.get(WorkStep, step_id, populate_existing=True)
        # This row is the existing per-attempt dispatch fence. Concurrent
        # deliveries of the same claimed attempt serialize here before either
        # one can create or reuse the physical call marker below.
        attempt = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
        if (
            order is None
            or order.status != "running"
            or step is None
            or step.work_order_id != order.id
            or attempt is None
            or not attempt_owns_lease(step, attempt)
        ):
            return False
        kind = step.kind
        durable_chat = order.source == "durable_chat"
        existing_call = await db.scalar(
            select(WorkToolCall).where(
                WorkToolCall.attempt_id == attempt.id,
                WorkToolCall.call_no == 1,
            )
        )
        if existing_call is not None:
            # A prepared/running/terminal call proves that this attempt already
            # crossed its dispatch boundary. It never authorizes replay, and a
            # newly introduced guard must not rewrite its unknown/recorded state.
            return False
        if kind == "agent_turn" and not durable_chat:
            # E21 headless safety retirement: this path could make provider
            # and nested tool calls outside the shared ledger and without a
            # recipient checkpoint. Stop under the authoritative order and
            # attempt locks, before creating a WorkToolCall marker or crossing
            # any model/tool dispatch boundary. Replanning the same unsupported
            # kind would only repeat the bypass under a new attempt.
            error = {
                "code": "headless_agent_turn_requires_durable_intake",
                "message": (
                    "Non-durable agent_turn execution is disabled; submit work "
                    "through the durable common intake"
                ),
                "type": "HeadlessAgentTurnDisabled",
            }
            now = utcnow()
            attempt.status = "failed"
            attempt.error = error
            attempt.finished_at = now
            attempt.heartbeat_at = now
            step.last_error = error
            step.next_attempt_at = None
            await transition_step(
                db,
                step,
                "failed",
                actor=str(attempt.worker_id),
                payload={"error": error},
            )
            order.blocker = {**error, "step_id": str(step.id)}
            await transition_work_order(
                db,
                order,
                "blocked",
                actor=str(attempt.worker_id),
                payload={"reason": error["code"], "step_id": str(step.id)},
            )
            await db.commit()
            return False
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
        # Ф1.B: hand this retry whatever checkpoint the step's last failed
        # attempt reported (see PartialProgressError / fail_attempt), so a
        # capability that supports it can resume instead of redoing work a
        # prior attempt already finished. A plan-defined ``_resume_checkpoint``
        # in the step's own static input always wins — this only fills the
        # gap when the plan didn't set one.
        if attempt.attempt_no > 1 and "_resume_checkpoint" not in input_data:
            from sqlalchemy import select as _select

            prior = (
                await db.execute(
                    _select(WorkStepAttempt.checkpoint)
                    .where(
                        WorkStepAttempt.step_id == step.id,
                        WorkStepAttempt.attempt_no < attempt.attempt_no,
                        WorkStepAttempt.checkpoint.is_not(None),
                    )
                    .order_by(WorkStepAttempt.attempt_no.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if prior is not None:
                input_data = {**input_data, "_resume_checkpoint": prior}
        # Ф4 (AGENT_AUTONOMY_ROADMAP.md): found live on the pilot —
        # computer_use's own parameter schema names work_order_id as
        # required, but the reasoning model generating the plan consistently
        # left it out of the step's input, 422ing every real web_discover
        # call. The durable runtime already knows this value authoritatively
        # (step.work_order_id) — making the model responsible for perfectly
        # echoing back a value the system already has was needless fragility
        # for zero benefit. Filled in here, once, for every capability step,
        # not special-cased to computer_use, so any future capability that
        # needs it is covered too; an explicit value the plan already set
        # always wins (e.g. a decompose child's own id, if that's ever a
        # real use case) — this only fills the gap when it's missing.
        if kind == "capability" and "work_order_id" not in input_data:
            input_data = {**input_data, "work_order_id": str(step.work_order_id)}
        timeout_seconds = step.timeout_seconds
        work_order_id = step.work_order_id
        owner_key = order.owner_key
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
            payload={
                "tool_call_id": str(call.id),
                "digest": digest,
                "capability": capability,
                "action": action,
            },
        )
        await db.commit()

    worker = str(attempt.worker_id)
    async with factory() as db:
        order_row = await db.get(WorkOrder, work_order_id, with_for_update=True)
        step_row = await db.get(WorkStep, step_id, with_for_update=True)
        attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
        if (
            order_row is None
            or order_row.status != "running"
            or step_row is None
            or attempt_row is None
            or not attempt_owns_lease(step_row, attempt_row)
        ):
            return False
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
    # Ф4 (AGENT_AUTONOMY_ROADMAP.md): found live on the pilot — nothing in this
    # durable runtime ever called set_acting_user, so every capability call a
    # WorkStep makes authenticated as the bare "agent-service" account (see
    # app.ai.actor_context's own docstring: "headless turns... leave it
    # unset"). Harmless for capabilities with no ownership check, but any
    # endpoint using get_effective_user to scope by the WorkOrder's owner
    # (computer_use's execute/web_discover, fixed in Ф2.A specifically for
    # this "agent acts for a human" case) 404'd on every WorkOrder not
    # literally owned by "agent-service" — which is all of them. Reset in
    # `finally` since this worker process/event loop may go on to execute
    # unrelated tasks after this one.
    from app.ai.actor_context import set_acting_user

    set_acting_user(owner_key)
    capability_budget_context = (
        WorkBudgetContext(
            work_order_id=work_order_id,
            step_id=step_id,
            attempt_id=attempt_id,
            session_factory=factory,
        )
        if kind == "capability"
        else None
    )
    try:
        if durable_chat:
            from app.tasks.durable_chat import run_durable_chat

            output = await run_durable_chat(
                work_order_id, step_id, attempt_id, session_factory=factory
            )
        else:
            output = await _execute_step_kind(
                kind,
                input_data,
                timeout_seconds,
                capability=capability,
                action=action,
                idempotency_key=step_idempotency_key,
                work_order_id=work_order_id,
                budget_context=capability_budget_context,
            )
    except ApprovalRequiredError as exc:
        from app.db.models import Approval, ApprovalActionType

        # Bind every durable approval to the arguments this attempt actually
        # sent.  A recipient envelope never supplies or widens that digest.
        expected_arguments = dict(input_data)
        expected_arguments.pop("approval", None)
        if (
            exc.capability != capability
            or exc.action != action
            or exc.arguments != expected_arguments
        ):
            error = {
                "code": "approval_call_mismatch",
                "message": "Approval request did not match the current capability call",
            }
            async with factory() as db:
                order = await db.get(WorkOrder, work_order_id, with_for_update=True)
                step_row = await db.get(WorkStep, step_id, with_for_update=True)
                attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
                call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
                if order and step_row and attempt_row and attempt_owns_lease(step_row, attempt_row):
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
        digest = _action_digest(exc.capability, exc.action, exc.arguments)
        async with factory() as db:
            order = await db.get(WorkOrder, work_order_id, with_for_update=True)
            step_row = await db.get(WorkStep, step_id, with_for_update=True)
            attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
            call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
            if order and step_row and attempt_row and attempt_owns_lease(step_row, attempt_row):
                tool_result = exc.tool_result
                budget_error = exc.budget_stop.as_error() if exc.budget_stop is not None else None
                if (
                    tool_result is not None
                    and budget_error is not None
                    and isinstance(exc.recipient_output, dict)
                    and isinstance(exc.recipient_output.get("result"), dict)
                ):
                    tool_result = exc.recipient_output["result"]
                checkpoint = tool_result.get("checkpoint") if tool_result else None
                evidence = tool_result.get("evidence") if tool_result else None
                result_error = (
                    {
                        "code": "tool_result_waiting_approval",
                        "error_code": tool_result.get("error_code"),
                        "evidence": evidence if isinstance(evidence, dict) else {},
                    }
                    if tool_result is not None
                    else None
                )
                if budget_error is not None:
                    result_error = {
                        **(result_error or {"code": "approval_required"}),
                        "budget_error": budget_error,
                    }
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
                        **({"tool_result": tool_result} if tool_result is not None else {}),
                        **(
                            {
                                "recipient_output": exc.recipient_output,
                                "budget_error": budget_error,
                            }
                            if budget_error is not None
                            else {}
                        ),
                    },
                )
                db.add(approval)
                await db.flush()
                attempt_row.status = "waiting_approval"
                attempt_row.finished_at = utcnow()
                if tool_result is not None:
                    attempt_row.output = tool_result
                    attempt_row.checkpoint = checkpoint if isinstance(checkpoint, dict) else None
                    attempt_row.error = result_error
                    step_row.output = {"result": tool_result, "executor": "capability"}
                    step_row.last_error = result_error
                elif budget_error is not None and exc.recipient_output is not None:
                    attempt_row.output = exc.recipient_output
                    attempt_row.error = result_error
                    step_row.output = exc.recipient_output
                    step_row.last_error = result_error
                if call_row is not None:
                    call_row.status = "waiting_approval"
                    if tool_result is not None:
                        call_row.output = (
                            exc.recipient_output
                            if budget_error is not None and exc.recipient_output is not None
                            else tool_result
                        )
                        call_row.error = result_error
                    elif budget_error is not None and exc.recipient_output is not None:
                        call_row.output = exc.recipient_output
                        call_row.error = result_error
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
                if exc.capability == "computer_use":
                    # Ф2.B (AGENT_AUTONOMY_ROADMAP.md): deciding the Approval
                    # row above (X-Agent-Approval: granted on retry) is not
                    # enough by itself here — computer_use's own grant check
                    # (ComputerUseGrant, a separate authorization primitive
                    # from the digest-Approval one) still 423s without an
                    # active grant, and only a manager can create one
                    # (POST /work-orders/{id}/computer-grants). This exception
                    # alone can't tell "no grant at all" apart from "a gated
                    # computer_use action (shell/file_write/desktop_*) needs
                    # its digest approved" — sent for both rather than
                    # silently leaving the no-grant case with no signal at
                    # all; a spurious nudge when a grant already exists costs
                    # a manager one glance, silence costs the WorkOrder
                    # stalling with no indication why. Must run BEFORE this
                    # transaction's commit below — create_notification only
                    # adds+flushes, it doesn't commit, so calling it after
                    # commit() silently loses the row when this session exits.
                    await _notify_computer_use_needs_grant(
                        db, order=order, capability_action=exc.action, reason=str(exc)
                    )
                await db.commit()
        return False
    except PartialProgressError as exc:
        # Ф1.B: distinct from the generic transient-error path only in that
        # it carries a checkpoint to persist — always retryable, same as a
        # transient error, so the next attempt (which will pick this
        # checkpoint up, see the resume_step_input merge above) gets a chance
        # to build on it rather than start over.
        error = {"code": "partial_progress", "message": str(exc), "type": type(exc).__name__}
        if exc.budget_stop is not None:
            error["budget_error"] = exc.budget_stop.as_error()
        async with factory() as db:
            order = await db.get(WorkOrder, work_order_id, with_for_update=True)
            step_row = await db.get(WorkStep, step_id, with_for_update=True)
            attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
            call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
            if order and step_row and attempt_row and attempt_owns_lease(step_row, attempt_row):
                if exc.budget_stop is None:
                    await fail_attempt(
                        db,
                        order=order,
                        step=step_row,
                        attempt=attempt_row,
                        error=error,
                        retryable=True,
                        actor=worker,
                        checkpoint=exc.checkpoint,
                    )
                else:
                    now = utcnow()
                    budget_error = exc.budget_stop.as_error()
                    attempt_row.status = "failed"
                    attempt_row.output = exc.recipient_output
                    attempt_row.checkpoint = exc.checkpoint
                    attempt_row.error = error
                    attempt_row.finished_at = now
                    attempt_row.heartbeat_at = now
                    step_row.output = exc.recipient_output
                    step_row.last_error = error
                    step_row.lease_owner = None
                    step_row.lease_expires_at = None
                    step_row.next_attempt_at = None
                    await transition_step(
                        db,
                        step_row,
                        "failed",
                        actor=worker,
                        payload={"error": error},
                    )
                    order.blocker = {**budget_error, "step_id": str(step_row.id)}
                    await transition_work_order(
                        db,
                        order,
                        "blocked",
                        actor=worker,
                        payload={"budget_error": budget_error},
                    )
                if call_row is not None:
                    call_row.status = "failed"
                    if exc.recipient_output is not None:
                        call_row.output = exc.recipient_output
                    call_row.error = error
                    call_row.finished_at = utcnow()
                await db.commit()
        return False
    except ChatWaitingApprovalToolResult as exc:
        from app.db.models import Approval, ApprovalActionType, ApprovalStatus

        async with factory() as db:
            order = await db.get(WorkOrder, work_order_id, with_for_update=True)
            step_row = await db.get(WorkStep, step_id, with_for_update=True)
            attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
            call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
            if order and step_row and attempt_row and attempt_owns_lease(step_row, attempt_row):
                try:
                    approval_context = await validate_recorded_recipient_binding(
                        db,
                        order=order,
                        step=step_row,
                        attempt=attempt_row,
                        action_id=exc.action_id,
                        call_id=exc.call_id,
                        result=exc.result,
                        expected_request=exc.function,
                        lock_action=True,
                    )
                except (TypeError, ValueError, WorkStateError):
                    error = {
                        "code": "recorded_recipient_approval_binding_invalid",
                        "message": "Recorded recipient approval binding changed",
                    }
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
                approvals = list(
                    await db.scalars(
                        select(Approval)
                        .where(
                            Approval.entity_id == order.id,
                            Approval.entity_type == "work_order",
                            Approval.action_type == ApprovalActionType.agent_tool_call,
                            Approval.status == ApprovalStatus.pending,
                        )
                        .with_for_update()
                    )
                )
                foreign_approvals = list(
                    await db.scalars(
                        select(Approval)
                        .where(
                            Approval.entity_id == order.id,
                            Approval.entity_type != "work_order",
                            Approval.action_type == ApprovalActionType.agent_tool_call,
                            Approval.status == ApprovalStatus.pending,
                        )
                        .with_for_update()
                    )
                )
                bound = [
                    item
                    for item in approvals
                    if (item.context or {}).get("continuation_mode") == "recorded_recipient_result"
                    and (
                        (item.context or {}).get("source_attempt_id") == str(attempt_row.id)
                        or (item.context or {}).get("action_id") == exc.action_id
                    )
                ]
                exact = [
                    item
                    for item in bound
                    if item.entity_type == "work_order" and item.context == approval_context
                ]
                foreign_bound = [
                    item
                    for item in foreign_approvals
                    if (item.context or {}).get("continuation_mode") == "recorded_recipient_result"
                    and (
                        (item.context or {}).get("source_attempt_id") == str(attempt_row.id)
                        or (item.context or {}).get("action_id") == exc.action_id
                    )
                ]
                if foreign_bound or len(bound) > 1 or (bound and len(exact) != 1):
                    error = {
                        "code": "recorded_recipient_approval_binding_invalid",
                        "message": "Existing recipient approval binding is ambiguous or changed",
                    }
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
                approval = exact[0] if exact else None
                if approval is None:
                    approval = Approval(
                        action_type=ApprovalActionType.agent_tool_call,
                        entity_type="work_order",
                        entity_id=order.id,
                        requested_by=order.owner_key,
                        context=approval_context,
                    )
                    db.add(approval)
                    await db.flush()
                # The source call already ran.  Preserve its recorded result
                # and durable checkpoint, then wait for an approval without
                # converting it into retryable work or authorizing replay.
                error = {
                    "code": "tool_result_waiting_approval",
                    "error_code": exc.result.get("error_code"),
                    "evidence": exc.result.get("evidence") or {},
                }
                attempt_row.status = "waiting_approval"
                attempt_row.output = exc.result
                attempt_row.error = error
                attempt_row.finished_at = utcnow()
                attempt_row.heartbeat_at = utcnow()
                step_row.output = {"result": exc.result, "executor": "durable_chat"}
                step_row.last_error = error
                step_row.lease_owner = None
                step_row.lease_expires_at = None
                await transition_step(
                    db,
                    step_row,
                    "waiting_approval",
                    actor=worker,
                    payload={"approval_id": str(approval.id), "action_id": exc.action_id},
                )
                await transition_work_order(
                    db,
                    order,
                    "waiting_approval",
                    actor=worker,
                    payload={"approval_id": str(approval.id), "step_id": str(step_row.id)},
                )
                if call_row is not None:
                    call_row.status = "waiting_approval"
                    call_row.output = exc.result
                    call_row.error = error
                    call_row.finished_at = utcnow()
                await append_event(
                    db,
                    order.id,
                    "chat.recorded_recipient_approval_requested",
                    actor=worker,
                    payload={
                        "approval_id": str(approval.id),
                        "action_id": exc.action_id,
                        "call_id": exc.call_id,
                        "request_digest": approval_context["request_digest"],
                        "result_digest": approval_context["result_digest"],
                    },
                )
                await db.commit()
        return False
    except (NonterminalToolResultError, ChatNonterminalToolResult) as exc:
        async with factory() as db:
            order = await db.get(WorkOrder, work_order_id, with_for_update=True)
            step_row = await db.get(WorkStep, step_id, with_for_update=True)
            attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
            call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
            if order and step_row and attempt_row and attempt_owns_lease(step_row, attempt_row):
                await stop_attempt_for_nonterminal_tool_result(
                    db,
                    order=order,
                    step=step_row,
                    attempt=attempt_row,
                    result=exc.result,
                    actor=worker,
                )
                if isinstance(exc, NonterminalToolResultError) and exc.budget_stop is not None:
                    budget_error = exc.budget_stop.as_error()
                    if isinstance(exc.recipient_output, dict) and isinstance(
                        exc.recipient_output.get("result"), dict
                    ):
                        observed_result = exc.recipient_output["result"]
                        attempt_row.output = observed_result
                        step_row.output = {"result": observed_result, "executor": "capability"}
                    attempt_row.error = {**(attempt_row.error or {}), "budget_error": budget_error}
                    step_row.last_error = {
                        **(step_row.last_error or {}),
                        "budget_error": budget_error,
                    }
                    order.blocker = {**(order.blocker or {}), "budget_error": budget_error}
                if call_row is not None:
                    call_row.status = str(exc.result["status"])
                    call_row.output = (
                        exc.recipient_output["result"]
                        if isinstance(exc, NonterminalToolResultError)
                        and exc.budget_stop is not None
                        and isinstance(exc.recipient_output, dict)
                        and isinstance(exc.recipient_output.get("result"), dict)
                        else exc.result
                    )
                    call_row.error = {
                        "code": f"tool_result_{exc.result['status']}",
                        "error_code": exc.result.get("error_code"),
                        "evidence": exc.result.get("evidence") or {},
                    }
                    if isinstance(exc, NonterminalToolResultError) and exc.budget_stop is not None:
                        call_row.error["budget_error"] = exc.budget_stop.as_error()
                        if exc.recipient_output is not None:
                            call_row.error["recipient_http_status"] = exc.recipient_output.get(
                                "http_status"
                            )
                    call_row.finished_at = utcnow()
                await db.commit()
        return False
    except BudgetExecutionStopped as exc:
        if exc.code == "llm_execution_already_started":
            # A duplicate delivery shares the winning attempt and lease. The
            # loser must not fail or unblock that state while the winner runs.
            return False
        error = {**exc.as_error(), "type": type(exc).__name__}
        recipient_output = getattr(exc, "recipient_output", None)
        recipient_confirmed = (
            recipient_output.get("recipient_confirmed")
            if isinstance(recipient_output, dict)
            else None
        )
        if recipient_confirmed is not None:
            error["recipient_confirmed"] = recipient_confirmed
        if isinstance(recipient_output, dict) and recipient_output.get("transport_error_type"):
            error["transport_error_type"] = recipient_output["transport_error_type"]
        async with factory() as db:
            order = await db.get(WorkOrder, work_order_id, with_for_update=True)
            step_row = await db.get(WorkStep, step_id, with_for_update=True)
            attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
            call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
            if order and step_row and attempt_row and attempt_owns_lease(step_row, attempt_row):
                now = utcnow()
                attempt_row.status = "failed"
                attempt_row.error = error
                if recipient_confirmed is True:
                    attempt_row.output = recipient_output
                    result = recipient_output.get("result")
                    if isinstance(result, dict) and isinstance(result.get("checkpoint"), dict):
                        attempt_row.checkpoint = result["checkpoint"]
                attempt_row.finished_at = now
                attempt_row.heartbeat_at = now
                step_row.last_error = error
                if recipient_confirmed is True:
                    step_row.output = recipient_output
                step_row.lease_owner = None
                step_row.lease_expires_at = None
                step_row.next_attempt_at = None
                await transition_step(
                    db,
                    step_row,
                    "failed",
                    actor=worker,
                    payload={"error": error},
                )
                order.blocker = error
                await transition_work_order(
                    db,
                    order,
                    "blocked",
                    actor=worker,
                    payload={"budget_error": error},
                )
                if call_row is not None:
                    call_row.status = (
                        "failed" if recipient_confirmed is not False else "outcome_unknown"
                    )
                    if isinstance(recipient_output, dict):
                        call_row.output = recipient_output
                    call_row.error = error
                    call_row.finished_at = now
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
            if order and step_row and attempt_row and attempt_owns_lease(step_row, attempt_row):
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
            if order and step_row and attempt_row and attempt_owns_lease(step_row, attempt_row):
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
        set_acting_user(None)

    async with factory() as db:
        order = await db.get(WorkOrder, work_order_id, with_for_update=True)
        step_row = await db.get(WorkStep, step_id, with_for_update=True)
        attempt_row = await db.get(WorkStepAttempt, attempt_id, with_for_update=True)
        call_row = await db.get(WorkToolCall, call_id, with_for_update=True)
        if (
            not order
            or not step_row
            or not attempt_row
            or not attempt_owns_lease(step_row, attempt_row)
        ):
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

    from app.db.models import WorkOrder
    from app.db.session import _get_session_factory
    from app.domain.work_orders import (
        find_active_plan_succeeded_steps,
        unstick_ready_orders_with_stalled_active_plan,
    )

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
        await promote_waiting_parents(db)
        # Ф4 (AGENT_AUTONOMY_ROADMAP.md): must run before
        # find_active_plan_succeeded_steps below — recovers a "ready" order
        # whose active plan finished stepping but was never handed back for
        # verification (see the function's own docstring), so its succeeded
        # step is picked up by the very next query in this same pass instead
        # of waiting a whole extra 5s tick.
        await unstick_ready_orders_with_stalled_active_plan(db)
        pending_verification = await find_active_plan_succeeded_steps(db, limit=100)
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


async def _plan_order(work_order_id: uuid.UUID, *, session_factory: Any | None = None) -> bool:
    from app.db.session import _get_session_factory
    from app.domain.work_planning import plan_work_order_detached

    factory = session_factory or _get_session_factory()
    return await plan_work_order_detached(work_order_id, session_factory=factory)


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


@celery_app.task(
    name="work.detect_gaps",
    queue="scheduler",
    max_retries=0,
    ignore_result=True,
)
def detect_capability_gaps_task() -> None:
    """Б12: batched, periodic — never synchronous per failed attempt."""
    from app.domain.work_gap_detection import run_gap_detection

    run_async(run_gap_detection())
