"""Per-execution LLM budget boundary for durable agent turns."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select

from app.db.models import WorkOrder, WorkPlan, WorkStep, WorkStepAttempt
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.work_budget_ledger import (
    BudgetBindingConflict,
    BudgetExceeded,
    BudgetLedgerError,
    BudgetReservationConflict,
    reserve_budget_for_dispatch,
    settle_budget,
)
from app.domain.work_orders import attempt_owns_lease


class BudgetExecutionStopped(BaseException):
    """Terminal, non-retryable durable execution boundary.

    ``BaseException`` is intentional: model recovery/fallback handlers commonly
    catch ``Exception`` and must never turn a budget stop into model prose or a
    second provider attempt.
    """

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_error(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


@dataclass
class _ExecutionState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    logical_call_no: int = 0
    physical_call_no: int = 0
    physical_tool_attempt_no: int = 0
    execution_claimed: bool = False
    stopped: BudgetExecutionStopped | None = None


@dataclass
class _RecipientExecutionState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    logical_call_no: int = 0
    physical_call_no: int = 0
    recipient_claimed: bool = False
    stopped: BudgetExecutionStopped | None = None


@asynccontextmanager
async def _recipient_validation_session(factory, existing=None):
    if existing is not None:
        yield existing
    else:
        async with factory() as db:
            yield db


@dataclass(frozen=True)
class WorkBudgetContext:
    """Authoritative durable IDs plus task-local physical-call ordinals.

    ``session_factory`` is the injected project factory (configured with
    ``expire_on_commit=False``); it is part of this immutable execution
    context rather than process-global mutable state.
    """

    work_order_id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID
    session_factory: Any = field(repr=False, compare=False)
    expected_owner_key: str | None = None
    expected_plan_id: uuid.UUID | None = None
    expected_plan_revision: int | None = None
    expected_ledger_id: uuid.UUID | None = None
    _state: _ExecutionState = field(default_factory=_ExecutionState, repr=False, compare=False)

    async def _stop(self, error: BudgetExecutionStopped) -> BudgetExecutionStopped:
        async with self._state.lock:
            if self._state.stopped is None:
                self._state.stopped = error
            return self._state.stopped

    def raise_if_stopped(self) -> None:
        if self._state.stopped is not None:
            raise self._state.stopped

    async def _active_ledger(self) -> WorkBudgetLedger:
        try:
            async with self.session_factory() as db:
                order = await db.get(WorkOrder, self.work_order_id, with_for_update=True)
                step = await db.get(WorkStep, self.step_id)
                attempt = await db.get(WorkStepAttempt, self.attempt_id)
                if order is None or order.budget_ledger_id is None:
                    raise await self._stop(
                        BudgetExecutionStopped(
                            "legacy_budget_baseline_required",
                            "Durable work has no intake-time budget ledger; explicit legacy "
                            "baseline reconciliation is required",
                        )
                    )
                ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id)
                if ledger is None or ledger.owner_key != order.owner_key:
                    raise await self._stop(
                        BudgetExecutionStopped(
                            "budget_binding_invalid",
                            "Durable work budget binding is missing or does not match its owner",
                        )
                    )
                plan = await db.get(WorkPlan, step.plan_id) if step is not None else None
                frozen_binding_valid = self.expected_owner_key is None or (
                    order.owner_key == self.expected_owner_key
                    and order.budget_ledger_id == self.expected_ledger_id
                    and ledger.id == self.expected_ledger_id
                    and self.expected_plan_id is not None
                    and self.expected_plan_revision is not None
                    and step is not None
                    and step.plan_id == self.expected_plan_id
                    and plan is not None
                    and plan.id == self.expected_plan_id
                    and plan.work_order_id == order.id
                    and plan.status == "active"
                    and plan.revision == self.expected_plan_revision
                    and order.plan_revision == self.expected_plan_revision
                )
                if (
                    order.status != "running"
                    or step is None
                    or step.work_order_id != order.id
                    or attempt is None
                    or attempt.step_id != step.id
                    or not attempt_owns_lease(step, attempt)
                    or not frozen_binding_valid
                ):
                    raise await self._stop(
                        BudgetExecutionStopped(
                            "budget_execution_inactive",
                            "Execution stopped or lease expired before the provider call",
                        )
                    )
                return ledger
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_state_unavailable",
                    "Durable budget state could not be verified; provider dispatch is forbidden",
                    details={"budget_error": str(exc)},
                )
            ) from exc

    async def assert_ready(self) -> None:
        """Claim this attempt execution once and reject duplicate workers."""
        self.raise_if_stopped()
        await self._active_ledger()
        async with self._state.lock:
            if self._state.stopped is not None:
                raise self._state.stopped
            if self._state.execution_claimed:
                return
            operation_key = f"execution:{self.attempt_id}"
            request_digest = hashlib.sha256(
                json.dumps(
                    {
                        "work_order_id": str(self.work_order_id),
                        "step_id": str(self.step_id),
                        "attempt_id": str(self.attempt_id),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            try:
                _, created = await reserve_budget_for_dispatch(
                    self.session_factory,
                    work_order_id=self.work_order_id,
                    operation_key=operation_key,
                    dimension="llm_calls",
                    units=0,
                    request_digest=request_digest,
                )
            except Exception as exc:
                error = BudgetExecutionStopped(
                    "llm_execution_fence_failed",
                    "Durable execution fence could not be committed; provider dispatch is forbidden",
                    details={"budget_error": str(exc)},
                )
                if self._state.stopped is None:
                    self._state.stopped = error
                raise self._state.stopped from exc
            if not created:
                error = BudgetExecutionStopped(
                    "llm_execution_already_started",
                    "This durable attempt already crossed its execution boundary; replay is forbidden",
                    details={"operation_key": operation_key},
                )
                if self._state.stopped is None:
                    self._state.stopped = error
                raise self._state.stopped
            self._state.execution_claimed = True
        # A cancellation can win immediately after the independently committed
        # marker. The marker remains as crash/replay evidence.
        await self._active_ledger()

    async def begin_logical_call(self) -> int:
        await self.assert_ready()
        async with self._state.lock:
            if self._state.stopped is not None:
                raise self._state.stopped
            self._state.logical_call_no += 1
            return self._state.logical_call_no

    async def _assert_llm_budget_bounds(
        self,
        ledger: WorkBudgetLedger,
        *,
        path_name: str,
    ) -> None:
        """Reject unprovable totals and exhausted shared call slots."""
        if ledger.max_tokens is not None:
            raise await self._stop(
                BudgetExecutionStopped(
                    "token_budget_enforcement_unavailable",
                    f"A finite token budget cannot be proven before this {path_name} call",
                    details={"dimension": "tokens", "limit": str(ledger.max_tokens)},
                )
            )
        if ledger.max_cost_usd is not None:
            raise await self._stop(
                BudgetExecutionStopped(
                    "cost_budget_enforcement_unavailable",
                    f"A finite cost budget cannot be proven before this {path_name} call",
                    details={"dimension": "cost_usd", "limit": str(ledger.max_cost_usd)},
                )
            )
        try:
            async with self.session_factory() as db:
                amount = case(
                    (
                        WorkBudgetReservation.state == "charged",
                        WorkBudgetReservation.actual_units,
                    ),
                    else_=WorkBudgetReservation.reserved_units,
                )
                used = Decimal(
                    await db.scalar(
                        select(func.coalesce(func.sum(amount), 0)).where(
                            WorkBudgetReservation.ledger_id == ledger.id,
                            WorkBudgetReservation.dimension == "llm_calls",
                        )
                    )
                    or 0
                )
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_state_unavailable",
                    f"{path_name.capitalize()} budget state could not be checked; "
                    "provider preparation is forbidden",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        if used >= Decimal(ledger.max_llm_calls):
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_call_budget_exceeded",
                    "LLM call budget is exhausted",
                    details={"used_or_reserved": str(used)},
                )
            )

    async def preflight_direct_text_call(self, provider: str | None) -> None:
        """Validate a direct text helper before GPU or client preparation."""
        self.raise_if_stopped()
        if (
            self.expected_owner_key is None
            or self.expected_plan_id is None
            or self.expected_plan_revision is None
            or self.expected_ledger_id is None
        ):
            raise await self._stop(
                BudgetExecutionStopped(
                    "direct_text_budget_context_invalid",
                    "Direct text execution lacks a frozen owner/plan binding",
                )
            )
        if provider != "ollama":
            raise await self._stop(
                BudgetExecutionStopped(
                    "direct_text_provider_unsupported",
                    "This durable direct text provider has no proven physical-attempt boundary",
                    details={"provider": str(provider or "")},
                )
            )
        ledger = await self._active_ledger()
        await self._assert_llm_budget_bounds(ledger, path_name="direct text")

    async def begin_direct_text_call(self, *, provider: str | None) -> int:
        """Start one logical direct-helper call on the durable execution."""
        await self.preflight_direct_text_call(provider)
        return await self.begin_logical_call()

    async def assert_direct_text_current(self) -> None:
        """Postflight fence before a direct response or retry can be applied."""
        await self._active_ledger()

    async def reject_explicit_budget_context(self) -> None:
        """Make an explicit/ambient context collision a sticky execution stop."""
        raise await self._stop(
            BudgetExecutionStopped(
                "budget_context_collision",
                "Explicit detached and ambient durable budget contexts cannot be combined",
            )
        )

    async def begin_airouter_call(
        self, *, provider: str, task: str, has_images: bool = False
    ) -> int:
        """Validate the narrow durable AIRouter slice before provider preparation."""
        self.raise_if_stopped()
        if (
            self.expected_owner_key is None
            or self.expected_plan_id is None
            or self.expected_plan_revision is None
            or self.expected_ledger_id is None
        ):
            raise await self._stop(
                BudgetExecutionStopped(
                    "airouter_budget_context_invalid",
                    "AIRouter durable execution lacks a frozen owner/plan binding",
                )
            )
        supported_tasks = {
            "orchestrator_planning",
            "classification",
            "email_drafting",
            "code_generation",
        }
        if provider != "ollama" or task not in supported_tasks or has_images:
            raise await self._stop(
                BudgetExecutionStopped(
                    "airouter_provider_path_unsupported",
                    "This durable AIRouter provider path has no proven physical-attempt boundary",
                    details={"provider": provider, "task": task, "has_images": has_images},
                )
            )
        ledger = await self._active_ledger()
        await self._assert_llm_budget_bounds(ledger, path_name="AIRouter")
        return await self.begin_logical_call()

    async def assert_airouter_current(self) -> None:
        """Postflight fence before an AIRouter response or retry can be applied."""
        await self._active_ledger()

    async def prepare_provider_call(
        self,
        *,
        logical_call_no: int,
        provider: str,
        provider_attempt: int,
        request: dict[str, Any],
    ) -> str:
        """Reserve one physical call and return its stable operation key."""
        await self.assert_ready()
        ledger = await self._active_ledger()
        if ledger.max_tokens is not None:
            raise await self._stop(
                BudgetExecutionStopped(
                    "token_budget_enforcement_unavailable",
                    "A finite token budget cannot be proven before this provider call",
                    details={"dimension": "tokens", "limit": str(ledger.max_tokens)},
                )
            )
        if ledger.max_cost_usd is not None:
            raise await self._stop(
                BudgetExecutionStopped(
                    "cost_budget_enforcement_unavailable",
                    "A finite cost budget cannot be proven before this provider call",
                    details={"dimension": "cost_usd", "limit": str(ledger.max_cost_usd)},
                )
            )

        async with self._state.lock:
            if self._state.stopped is not None:
                raise self._state.stopped
            self._state.physical_call_no += 1
            physical_call_no = self._state.physical_call_no
        provider_digest = hashlib.sha256(provider.encode()).hexdigest()[:12]
        operation_key = (
            f"llm:{self.attempt_id}:l{logical_call_no}:p{physical_call_no}:"
            f"{provider_digest}:r{provider_attempt}"
        )
        request_digest = hashlib.sha256(
            json.dumps(
                request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                default=str,
            ).encode()
        ).hexdigest()
        try:
            _, created = await reserve_budget_for_dispatch(
                self.session_factory,
                work_order_id=self.work_order_id,
                operation_key=operation_key,
                dimension="llm_calls",
                units=1,
                request_digest=request_digest,
            )
        except BudgetExceeded as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_call_budget_exceeded",
                    "LLM call budget is exhausted",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        except (BudgetBindingConflict, BudgetReservationConflict, BudgetLedgerError) as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_reservation_failed",
                    "LLM call budget reservation failed",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_reservation_unavailable",
                    "LLM call budget reservation could not be committed; provider dispatch is forbidden",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        if not created:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_provider_operation_already_recorded",
                    "This physical provider operation was already reserved or charged; "
                    "automatic replay is forbidden",
                    details={"operation_key": operation_key},
                )
            )

        # Cancellation may win immediately after the independently committed
        # reservation. Recheck the live lease before crossing the provider
        # boundary; the reservation remains consumed if execution stopped.
        await self._active_ledger()
        return operation_key

    async def charge_provider_call(self, operation_key: str) -> None:
        """Charge one dispatched attempt, whether it returned or raised."""
        try:
            await settle_budget(
                self.session_factory,
                work_order_id=self.work_order_id,
                operation_key=operation_key,
                actual_units=1,
            )
        except (BudgetBindingConflict, BudgetReservationConflict, BudgetLedgerError) as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_settlement_failed",
                    "LLM call was dispatched but its budget charge could not be settled",
                    details={"budget_error": str(exc), "operation_key": operation_key},
                )
            ) from exc
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_settlement_unavailable",
                    "LLM call was dispatched but its budget charge could not be committed",
                    details={"budget_error": str(exc), "operation_key": operation_key},
                )
            ) from exc

    async def prepare_tool_attempt(
        self,
        *,
        method: str,
        url: str,
        request: dict[str, Any],
    ) -> str:
        """Reserve one physical nested-tool attempt before transport dispatch."""
        await self.assert_ready()
        await self._active_ledger()
        async with self._state.lock:
            if self._state.stopped is not None:
                raise self._state.stopped
            self._state.physical_tool_attempt_no += 1
            physical_attempt_no = self._state.physical_tool_attempt_no

        request_digest = hashlib.sha256(
            json.dumps(
                {"method": method, "url": url, "request": request},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                default=str,
            ).encode()
        ).hexdigest()
        operation_key = (
            f"tool:{self.attempt_id}:p{physical_attempt_no}:{method.lower()}:{request_digest[:12]}"
        )
        try:
            _, created = await reserve_budget_for_dispatch(
                self.session_factory,
                work_order_id=self.work_order_id,
                operation_key=operation_key,
                dimension="tool_attempts",
                units=1,
                request_digest=request_digest,
            )
        except BudgetExceeded as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "tool_attempt_budget_exceeded",
                    "Tool attempt budget is exhausted",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        except (BudgetBindingConflict, BudgetReservationConflict, BudgetLedgerError) as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "tool_budget_reservation_failed",
                    "Tool attempt budget reservation failed",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "tool_budget_reservation_unavailable",
                    "Tool attempt budget reservation could not be committed; dispatch is forbidden",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        if not created:
            raise await self._stop(
                BudgetExecutionStopped(
                    "tool_operation_already_recorded",
                    "This physical tool operation was already reserved or charged; "
                    "automatic replay is forbidden",
                    details={"operation_key": operation_key},
                )
            )

        # A cancellation can win after the independently committed reservation.
        # Keep that reservation consumed, but recheck the lease before transport.
        await self._active_ledger()
        return operation_key

    async def charge_tool_attempt(
        self,
        operation_key: str,
        *,
        recipient_outcome: str,
    ) -> bool:
        """Charge one dispatched attempt without hiding its recipient outcome.

        A settlement failure is sticky but deliberately deferred. The caller can
        first persist the already-observed ToolResult/checkpoint, then cross the
        typed stop before another tool or model call.
        """
        try:
            await settle_budget(
                self.session_factory,
                work_order_id=self.work_order_id,
                operation_key=operation_key,
                actual_units=1,
            )
            return True
        except (BudgetBindingConflict, BudgetReservationConflict, BudgetLedgerError) as exc:
            await self._stop(
                BudgetExecutionStopped(
                    "tool_budget_settlement_failed",
                    "Tool was dispatched but its budget charge could not be settled",
                    details={
                        "budget_error": str(exc),
                        "operation_key": operation_key,
                        "recipient_outcome": recipient_outcome,
                        "consumed_budget": "reserved",
                    },
                )
            )
            return False
        except Exception as exc:
            await self._stop(
                BudgetExecutionStopped(
                    "tool_budget_settlement_unavailable",
                    "Tool was dispatched but its budget charge could not be committed",
                    details={
                        "budget_error": str(exc),
                        "operation_key": operation_key,
                        "recipient_outcome": recipient_outcome,
                        "consumed_budget": "reserved",
                    },
                )
            )
            return False

    async def create_http_recipient_handoff(
        self,
        *,
        method: str,
        path: str,
        body: dict[str, Any],
        tool_operation_key: str,
        actor: str,
    ) -> str:
        """Sign the one supported HTTP recipient after its tool reserve exists."""
        from datetime import UTC, datetime, timedelta

        from app.auth.work_budget_handoff import (
            WORKSPACE_SQL_TABLE_PATH,
            WorkBudgetHandoff,
            canonical_body_digest,
            sign_work_budget_handoff,
        )

        self.raise_if_stopped()
        if (
            method != "POST"
            or path != WORKSPACE_SQL_TABLE_PATH
            or not actor
            or actor != self.expected_owner_key
            or self.expected_ledger_id is None
            or self.expected_plan_id is None
            or self.expected_plan_revision is None
        ):
            raise await self._stop(
                BudgetExecutionStopped(
                    "http_recipient_handoff_unsupported",
                    "This durable HTTP recipient lacks a frozen supported handoff binding",
                )
            )
        await self._active_ledger()
        try:
            async with self.session_factory() as db:
                reservation = await db.scalar(
                    select(WorkBudgetReservation).where(
                        WorkBudgetReservation.ledger_id == self.expected_ledger_id,
                        WorkBudgetReservation.work_order_id == self.work_order_id,
                        WorkBudgetReservation.operation_key == tool_operation_key,
                    )
                )
                if (
                    reservation is None
                    or reservation.dimension != "tool_attempts"
                    or reservation.state != "reserved"
                ):
                    raise ValueError("parent tool reservation is not dispatchable")
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "http_recipient_handoff_state_invalid",
                    "The parent tool reservation cannot authorize this recipient",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        handoff = WorkBudgetHandoff(
            body_digest=canonical_body_digest(body),
            actor=actor,
            work_order_id=self.work_order_id,
            step_id=self.step_id,
            attempt_id=self.attempt_id,
            ledger_id=self.expected_ledger_id,
            plan_id=self.expected_plan_id,
            plan_revision=self.expected_plan_revision,
            tool_operation_key=tool_operation_key,
            tool_request_digest=reservation.request_digest,
            tool_binding_digest=reservation.binding_digest,
            expires_at=int((datetime.now(UTC) + timedelta(seconds=60)).timestamp()),
        )
        return sign_work_budget_handoff(handoff)

    async def record_http_recipient_stop(self, payload: dict[str, Any]) -> None:
        """Defer a recipient stop until its HTTP result can be checkpointed."""
        code = str(payload.get("error_code") or "http_recipient_budget_stopped")
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        await self._stop(
            BudgetExecutionStopped(
                code,
                str(evidence.get("message") or "HTTP recipient execution stopped"),
                details={"recipient_evidence": evidence},
            )
        )


@dataclass(frozen=True)
class RecipientWorkBudgetContext:
    """Recipient-owned physical LLM boundary for one signed SQL tool dispatch."""

    work_order_id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID
    owner_key: str
    ledger_id: uuid.UUID
    plan_id: uuid.UUID
    plan_revision: int
    tool_operation_key: str
    tool_request_digest: str
    tool_binding_digest: str
    body_digest: str
    session_factory: Any = field(repr=False, compare=False)
    _state: _RecipientExecutionState = field(
        default_factory=_RecipientExecutionState, repr=False, compare=False
    )

    async def _stop(self, error: BudgetExecutionStopped) -> BudgetExecutionStopped:
        async with self._state.lock:
            if self._state.stopped is None:
                self._state.stopped = error
            return self._state.stopped

    def raise_if_stopped(self) -> None:
        if self._state.stopped is not None:
            raise self._state.stopped

    @property
    def recipient_claimed(self) -> bool:
        return self._state.recipient_claimed

    @property
    def _scope_digest(self) -> str:
        return hashlib.sha256(self.tool_operation_key.encode()).hexdigest()[:24]

    async def _active_ledger(self, *, db=None) -> WorkBudgetLedger:
        self.raise_if_stopped()
        try:
            from app.db.models import User

            async with _recipient_validation_session(self.session_factory, db) as db:
                order = await db.get(WorkOrder, self.work_order_id)
                step = await db.get(WorkStep, self.step_id)
                attempt = await db.get(WorkStepAttempt, self.attempt_id)
                ledger = await db.get(WorkBudgetLedger, self.ledger_id)
                plan = await db.get(WorkPlan, self.plan_id)
                actor = await db.scalar(
                    select(User).where(User.sub == self.owner_key, User.is_active.is_(True))
                )
                parent_marker = await db.scalar(
                    select(WorkBudgetReservation).where(
                        WorkBudgetReservation.ledger_id == self.ledger_id,
                        WorkBudgetReservation.work_order_id == self.work_order_id,
                        WorkBudgetReservation.operation_key == f"execution:{self.attempt_id}",
                        WorkBudgetReservation.dimension == "llm_calls",
                        WorkBudgetReservation.reserved_units == 0,
                    )
                )
                tool_reservation = await db.scalar(
                    select(WorkBudgetReservation).where(
                        WorkBudgetReservation.ledger_id == self.ledger_id,
                        WorkBudgetReservation.work_order_id == self.work_order_id,
                        WorkBudgetReservation.operation_key == self.tool_operation_key,
                    )
                )
                valid = (
                    order is not None
                    and order.owner_key == self.owner_key
                    and order.status == "running"
                    and order.budget_ledger_id == self.ledger_id
                    and order.plan_revision == self.plan_revision
                    and ledger is not None
                    and ledger.owner_key == self.owner_key
                    and step is not None
                    and step.work_order_id == order.id
                    and step.plan_id == self.plan_id
                    and plan is not None
                    and plan.work_order_id == order.id
                    and plan.status == "active"
                    and plan.revision == self.plan_revision
                    and attempt is not None
                    and attempt.step_id == step.id
                    and attempt_owns_lease(step, attempt)
                    and actor is not None
                    and parent_marker is not None
                    and tool_reservation is not None
                    and tool_reservation.dimension == "tool_attempts"
                    and tool_reservation.state == "reserved"
                    and tool_reservation.request_digest == self.tool_request_digest
                    and tool_reservation.binding_digest == self.tool_binding_digest
                )
                if not valid:
                    raise ValueError("recipient binding, actor, lease, or reservation is stale")
                return ledger
        except BudgetExecutionStopped:
            raise
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "http_recipient_execution_inactive",
                    "HTTP recipient binding is stale; model dispatch or publication is forbidden",
                    details={"budget_error": str(exc)},
                )
            ) from exc

    async def assert_ready(self) -> None:
        await self._active_ledger()
        async with self._state.lock:
            if self._state.stopped is not None:
                raise self._state.stopped
            if self._state.recipient_claimed:
                return
            operation_key = f"recipient:{self.attempt_id}:{self._scope_digest}:once"
            request_digest = hashlib.sha256(
                json.dumps(
                    {
                        "work_order_id": str(self.work_order_id),
                        "step_id": str(self.step_id),
                        "attempt_id": str(self.attempt_id),
                        "tool_operation_key": self.tool_operation_key,
                        "body_digest": self.body_digest,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            try:
                _, created = await reserve_budget_for_dispatch(
                    self.session_factory,
                    work_order_id=self.work_order_id,
                    operation_key=operation_key,
                    dimension="llm_calls",
                    units=0,
                    request_digest=request_digest,
                )
            except Exception as exc:
                self._state.stopped = BudgetExecutionStopped(
                    "http_recipient_fence_failed",
                    "HTTP recipient once-fence could not be committed",
                    details={"budget_error": str(exc)},
                )
                raise self._state.stopped from exc
            if not created:
                self._state.stopped = BudgetExecutionStopped(
                    "http_recipient_already_started",
                    "This recipient dispatch already crossed its once-fence",
                    details={
                        "operation_key": operation_key,
                        "publication_state": "prior_outcome_unknown",
                        "dispatch_state": "not_dispatched_by_this_request",
                    },
                )
                raise self._state.stopped
            self._state.recipient_claimed = True
        await self._active_ledger()

    async def preflight_direct_text_call(self, provider: str | None) -> None:
        self.raise_if_stopped()
        if provider != "ollama":
            raise await self._stop(
                BudgetExecutionStopped(
                    "http_recipient_provider_unsupported",
                    "The SQL recipient supports only the proven Ollama physical boundary",
                    details={"provider": str(provider or "")},
                )
            )
        ledger = await self._active_ledger()
        await WorkBudgetContext._assert_llm_budget_bounds(self, ledger, path_name="HTTP recipient")
        await self.assert_ready()

    async def begin_direct_text_call(self, *, provider: str | None) -> int:
        await self.preflight_direct_text_call(provider)
        async with self._state.lock:
            self._state.logical_call_no += 1
            return self._state.logical_call_no

    async def prepare_provider_call(
        self,
        *,
        logical_call_no: int,
        provider: str,
        provider_attempt: int,
        request: dict[str, Any],
    ) -> str:
        if provider != "ollama":
            raise await self._stop(
                BudgetExecutionStopped(
                    "http_recipient_provider_unsupported",
                    "The SQL recipient supports only Ollama",
                )
            )
        await self.preflight_direct_text_call(provider)
        async with self._state.lock:
            if self._state.stopped is not None:
                raise self._state.stopped
            self._state.physical_call_no += 1
            physical_call_no = self._state.physical_call_no
        operation_key = (
            f"recipient-llm:{self.attempt_id}:{self._scope_digest}:"
            f"l{logical_call_no}:p{physical_call_no}:r{provider_attempt}"
        )
        request_digest = hashlib.sha256(
            json.dumps(
                request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                default=str,
            ).encode()
        ).hexdigest()
        try:
            _, created = await reserve_budget_for_dispatch(
                self.session_factory,
                work_order_id=self.work_order_id,
                operation_key=operation_key,
                dimension="llm_calls",
                units=1,
                request_digest=request_digest,
            )
        except BudgetExceeded as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_call_budget_exceeded",
                    "Shared LLM call budget is exhausted in the HTTP recipient",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        except (BudgetBindingConflict, BudgetReservationConflict, BudgetLedgerError) as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "http_recipient_llm_reservation_failed",
                    "Recipient LLM reservation failed",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "http_recipient_llm_reservation_unavailable",
                    "Recipient LLM reservation could not be committed",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        if not created:
            raise await self._stop(
                BudgetExecutionStopped(
                    "http_recipient_llm_operation_recorded",
                    "This recipient physical LLM operation was already recorded",
                    details={"operation_key": operation_key},
                )
            )
        await self._active_ledger()
        return operation_key

    async def charge_provider_call(self, operation_key: str) -> None:
        try:
            await settle_budget(
                self.session_factory,
                work_order_id=self.work_order_id,
                operation_key=operation_key,
                actual_units=1,
            )
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "http_recipient_llm_settlement_failed",
                    "Recipient LLM call was dispatched but could not be settled",
                    details={"budget_error": str(exc), "operation_key": operation_key},
                )
            ) from exc

    async def assert_direct_text_current(self) -> None:
        await self._active_ledger()

    async def assert_publish_current(self) -> None:
        await self._active_ledger()

    @asynccontextmanager
    async def publication_fence(self):
        """Keep cancellation and parent settlement behind the synchronous write."""
        from app.db.models import User

        async with self.session_factory() as db:
            async with db.begin():
                await db.get(WorkOrder, self.work_order_id, with_for_update=True)
                await db.get(WorkBudgetLedger, self.ledger_id, with_for_update=True)
                # Heartbeat/claim paths lock step first and later update order.
                # Do not introduce order->step or order->attempt inversion here.
                await db.scalar(select(User).where(User.sub == self.owner_key).with_for_update())
                await self._active_ledger(db=db)
                yield

    async def begin_airouter_call(self, **_kwargs) -> int:
        raise await self._stop(
            BudgetExecutionStopped(
                "http_recipient_airouter_unsupported",
                "AIRouter is outside the direct SQL recipient contract",
            )
        )

    async def reject_explicit_budget_context(self) -> None:
        raise await self._stop(
            BudgetExecutionStopped(
                "budget_context_collision",
                "Explicit detached and HTTP recipient budget contexts cannot be combined",
            )
        )


_airouter_budget_context: contextvars.ContextVar[
    WorkBudgetContext | RecipientWorkBudgetContext | None
] = contextvars.ContextVar("airouter_budget_context", default=None)


def current_airouter_budget_context() -> WorkBudgetContext | RecipientWorkBudgetContext | None:
    """Return the server-owned durable context for the current asyncio task."""
    return _airouter_budget_context.get()


@contextmanager
def bind_airouter_budget_context(context: WorkBudgetContext):
    """Bind one durable context without accepting request/metadata overrides."""
    current = _airouter_budget_context.get()
    if current is not None and current is not context:
        raise RuntimeError("AIRouter budget context is already bound")
    token = _airouter_budget_context.set(context)
    try:
        yield
    finally:
        _airouter_budget_context.reset(token)


@contextmanager
def bind_http_recipient_budget_context(context: RecipientWorkBudgetContext):
    """Replace task-local caller state only at the authenticated HTTP boundary."""
    token = _airouter_budget_context.set(context)
    try:
        yield
    finally:
        _airouter_budget_context.reset(token)


@dataclass(frozen=True)
class DetachedVerifierBudgetContext:
    """Shared-ledger fence for one authoritative verifier snapshot."""

    work_order_id: uuid.UUID
    owner_key: str
    snapshot_digest: str
    operation_scope: str
    session_factory: Any = field(repr=False, compare=False)
    snapshot_is_current: Callable[[], Awaitable[bool]] = field(repr=False, compare=False)
    _state: _ExecutionState = field(default_factory=_ExecutionState, repr=False, compare=False)

    async def _stop(self, error: BudgetExecutionStopped) -> BudgetExecutionStopped:
        async with self._state.lock:
            if self._state.stopped is None:
                self._state.stopped = error
            return self._state.stopped

    def raise_if_stopped(self) -> None:
        if self._state.stopped is not None:
            raise self._state.stopped

    async def assert_supported_provider(self, provider: str | None) -> None:
        self.raise_if_stopped()
        if provider != "ollama":
            raise await self._stop(
                BudgetExecutionStopped(
                    "verification_provider_unsupported",
                    "Budgeted semantic verification supports direct Ollama only",
                    details={"provider": str(provider or "")},
                )
            )

    async def _assert_current(self) -> None:
        self.raise_if_stopped()
        try:
            current = await self.snapshot_is_current()
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "verification_state_unavailable",
                    "Verifier state could not be revalidated; provider dispatch is forbidden",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        if not current:
            raise await self._stop(
                BudgetExecutionStopped(
                    "verification_snapshot_stale",
                    "Verifier inputs changed before provider dispatch",
                )
            )

    async def _assert_budget_bounds(self) -> None:
        try:
            async with self.session_factory() as db:
                order = await db.get(WorkOrder, self.work_order_id)
                if order is None or order.owner_key != self.owner_key:
                    raise BudgetBindingConflict("Verifier WorkOrder owner binding changed")
                if order.budget_ledger_id is None:
                    raise await self._stop(
                        BudgetExecutionStopped(
                            "legacy_budget_baseline_required",
                            "Work has no intake-time ledger; legacy reconciliation is required",
                        )
                    )
                ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id)
                if ledger is None or ledger.owner_key != self.owner_key:
                    raise BudgetBindingConflict("Verifier budget binding is invalid")
                if ledger.max_tokens is not None:
                    raise await self._stop(
                        BudgetExecutionStopped(
                            "token_budget_enforcement_unavailable",
                            "A finite total-token bound cannot be proven for this verifier call",
                            details={"dimension": "tokens", "limit": str(ledger.max_tokens)},
                        )
                    )
                if ledger.max_cost_usd is not None:
                    raise await self._stop(
                        BudgetExecutionStopped(
                            "cost_budget_enforcement_unavailable",
                            "A finite cost bound cannot be proven for this verifier call",
                            details={"dimension": "cost_usd", "limit": str(ledger.max_cost_usd)},
                        )
                    )
                amount = case(
                    (
                        WorkBudgetReservation.state == "charged",
                        WorkBudgetReservation.actual_units,
                    ),
                    else_=WorkBudgetReservation.reserved_units,
                )
                used = Decimal(
                    await db.scalar(
                        select(func.coalesce(func.sum(amount), 0)).where(
                            WorkBudgetReservation.ledger_id == ledger.id,
                            WorkBudgetReservation.dimension == "llm_calls",
                        )
                    )
                    or 0
                )
                if used >= Decimal(ledger.max_llm_calls):
                    raise await self._stop(
                        BudgetExecutionStopped(
                            "llm_call_budget_exceeded",
                            "LLM call budget is exhausted",
                            details={"used_or_reserved": str(used)},
                        )
                    )
        except BudgetExecutionStopped:
            raise
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_state_unavailable",
                    "Verifier budget state could not be checked; provider dispatch is forbidden",
                    details={"budget_error": str(exc)},
                )
            ) from exc

    async def _claim_once(self) -> None:
        async with self._state.lock:
            if self._state.stopped is not None:
                raise self._state.stopped
            if self._state.execution_claimed:
                return
            operation_key = f"verify:{self.operation_scope}:once"
            try:
                _, created = await reserve_budget_for_dispatch(
                    self.session_factory,
                    work_order_id=self.work_order_id,
                    operation_key=operation_key,
                    dimension="llm_calls",
                    units=0,
                    request_digest=self.snapshot_digest,
                )
            except Exception as exc:
                self._state.stopped = BudgetExecutionStopped(
                    "verification_fence_failed",
                    "Verifier fence could not be committed; provider dispatch is forbidden",
                    details={"budget_error": str(exc)},
                )
                raise self._state.stopped from exc
            if not created:
                self._state.stopped = BudgetExecutionStopped(
                    "verification_execution_already_started",
                    "This verifier snapshot already crossed its execution boundary",
                    details={"operation_key": operation_key},
                )
                raise self._state.stopped
            self._state.execution_claimed = True

    async def prepare_provider_call(
        self, *, provider: str, provider_attempt: int, request: dict[str, Any]
    ) -> str:
        await self.preflight_provider_call(provider)
        await self._claim_once()
        await self._assert_current()
        request_digest = hashlib.sha256(
            json.dumps(
                request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                default=str,
            ).encode()
        ).hexdigest()
        operation_key = f"verify:{self.operation_scope}:p{provider_attempt}"
        try:
            _, created = await reserve_budget_for_dispatch(
                self.session_factory,
                work_order_id=self.work_order_id,
                operation_key=operation_key,
                dimension="llm_calls",
                units=1,
                request_digest=request_digest,
            )
        except BudgetExceeded as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_call_budget_exceeded",
                    "LLM call budget is exhausted",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        except (BudgetBindingConflict, BudgetReservationConflict, BudgetLedgerError) as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_reservation_failed",
                    "Verifier LLM reservation failed",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_reservation_unavailable",
                    "Verifier LLM reservation could not be committed",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        if not created:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_provider_operation_already_recorded",
                    "This verifier provider attempt was already recorded",
                    details={"operation_key": operation_key},
                )
            )
        await self._assert_current()
        return operation_key

    async def preflight_provider_call(self, provider: str | None) -> None:
        """Fail closed before GPU/client preparation without consuming a slot."""
        await self.assert_supported_provider(provider)
        await self._assert_current()
        await self._assert_budget_bounds()

    async def charge_provider_call(self, operation_key: str) -> None:
        try:
            await settle_budget(
                self.session_factory,
                work_order_id=self.work_order_id,
                operation_key=operation_key,
                actual_units=1,
            )
        except (BudgetBindingConflict, BudgetReservationConflict, BudgetLedgerError) as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_settlement_failed",
                    "Verifier LLM call was dispatched but settlement failed",
                    details={"budget_error": str(exc), "operation_key": operation_key},
                )
            ) from exc
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_settlement_unavailable",
                    "Verifier LLM call was dispatched but settlement was unavailable",
                    details={"budget_error": str(exc), "operation_key": operation_key},
                )
            ) from exc
