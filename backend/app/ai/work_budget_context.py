"""Per-execution LLM budget boundary for durable agent turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.db.models import WorkOrder, WorkStep, WorkStepAttempt
from app.db.work_budget_models import WorkBudgetLedger
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
                if (
                    order.status != "running"
                    or step is None
                    or step.work_order_id != order.id
                    or attempt is None
                    or attempt.step_id != step.id
                    or not attempt_owns_lease(step, attempt)
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
