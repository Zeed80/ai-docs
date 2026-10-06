"""Shared-budget boundary for one detached capability-planner snapshot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select

from app.ai.work_budget_context import BudgetExecutionStopped
from app.db.models import WorkOrder
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.work_budget_ledger import (
    BudgetBindingConflict,
    BudgetExceeded,
    BudgetLedgerError,
    BudgetReservationConflict,
    reserve_budget_for_dispatch,
    settle_budget,
    settle_llm_call_with_usage_receipt,
)
from app.domain.work_budget_usage import OllamaUsageEvidence


@dataclass
class _PlannerState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    execution_claimed: bool = False
    stopped: BudgetExecutionStopped | None = None


@dataclass(frozen=True)
class DetachedPlannerBudgetContext:
    """Fence and account for physical calls made from one planner snapshot.

    Planning has no legitimate running ``WorkStepAttempt``.  The stable snapshot
    digest is therefore the execution identity; every reservation is committed
    independently before the corresponding provider dispatch.
    """

    work_order_id: uuid.UUID
    owner_key: str
    snapshot_digest: str
    prompt_digest: str
    operation_scope: str
    session_factory: Any = field(repr=False, compare=False)
    snapshot_is_current: Callable[[], Awaitable[bool]] = field(repr=False, compare=False)
    _state: _PlannerState = field(default_factory=_PlannerState, repr=False, compare=False)

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
        # Strata goes through generate_json's reserved/charged loop like Ollama.
        if provider not in ("ollama", "strata"):
            raise await self._stop(
                BudgetExecutionStopped(
                    "planning_provider_unsupported",
                    "Budgeted capability planning supports direct Ollama only",
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
                    "planning_state_unavailable",
                    "Planner state could not be revalidated; provider dispatch is forbidden",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        if not current:
            raise await self._stop(
                BudgetExecutionStopped(
                    "planning_snapshot_stale",
                    "Planner inputs changed before provider dispatch",
                )
            )

    async def _assert_budget_bounds(self) -> None:
        try:
            async with self.session_factory() as db:
                order = await db.get(WorkOrder, self.work_order_id)
                if order is None or order.owner_key != self.owner_key:
                    raise BudgetBindingConflict("Planner WorkOrder owner binding changed")
                if order.budget_ledger_id is None:
                    raise await self._stop(
                        BudgetExecutionStopped(
                            "legacy_budget_baseline_required",
                            "Work has no intake-time ledger; legacy reconciliation is required",
                        )
                    )
                ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id)
                if ledger is None or ledger.owner_key != self.owner_key:
                    raise BudgetBindingConflict("Planner budget binding is invalid")
                if ledger.max_tokens is not None:
                    raise await self._stop(
                        BudgetExecutionStopped(
                            "token_budget_enforcement_unavailable",
                            "A finite total-token bound cannot be proven for this planner call",
                            details={"dimension": "tokens", "limit": str(ledger.max_tokens)},
                        )
                    )
                if ledger.max_cost_usd is not None:
                    raise await self._stop(
                        BudgetExecutionStopped(
                            "cost_budget_enforcement_unavailable",
                            "A finite cost bound cannot be proven for this planner call",
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
                    "Planner budget state could not be checked; provider dispatch is forbidden",
                    details={"budget_error": str(exc)},
                )
            ) from exc

    async def _claim_once(self) -> None:
        async with self._state.lock:
            if self._state.stopped is not None:
                raise self._state.stopped
            if self._state.execution_claimed:
                return
            operation_key = f"plan:{self.operation_scope}:once"
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
                    "planning_fence_failed",
                    "Planner fence could not be committed; provider dispatch is forbidden",
                    details={"budget_error": str(exc)},
                )
                raise self._state.stopped from exc
            if not created:
                self._state.stopped = BudgetExecutionStopped(
                    "planning_execution_already_started",
                    "This planner snapshot already crossed its execution boundary",
                    details={"operation_key": operation_key},
                )
                raise self._state.stopped
            self._state.execution_claimed = True

    async def preflight_provider_call(self, provider: str | None) -> None:
        """Fail closed before GPU/client preparation without consuming a slot."""
        await self.assert_supported_provider(provider)
        await self._assert_current()
        await self._assert_budget_bounds()

    async def prepare_provider_call(
        self, *, provider: str, provider_attempt: int, request: dict[str, Any]
    ) -> str:
        await self.preflight_provider_call(provider)
        await self._claim_once()
        await self._assert_current()
        request_digest = hashlib.sha256(
            json.dumps(
                {"prompt_digest": self.prompt_digest, "request": request},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                default=str,
            ).encode()
        ).hexdigest()
        operation_key = f"plan:{self.operation_scope}:p{provider_attempt}"
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
                    "Planner LLM reservation failed",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_reservation_unavailable",
                    "Planner LLM reservation could not be committed",
                    details={"budget_error": str(exc)},
                )
            ) from exc
        if not created:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_provider_operation_already_recorded",
                    "This planner provider attempt was already recorded",
                    details={"operation_key": operation_key},
                )
            )
        await self._assert_current()
        return operation_key

    async def charge_provider_call(
        self,
        operation_key: str,
        *,
        usage_evidence: OllamaUsageEvidence | None = None,
    ) -> None:
        try:
            if usage_evidence is None:
                await settle_budget(
                    self.session_factory,
                    work_order_id=self.work_order_id,
                    operation_key=operation_key,
                    actual_units=1,
                )
            else:
                await settle_llm_call_with_usage_receipt(
                    self.session_factory,
                    work_order_id=self.work_order_id,
                    operation_key=operation_key,
                    expected_owner_key=self.owner_key,
                    evidence=usage_evidence,
                )
        except (BudgetBindingConflict, BudgetReservationConflict, BudgetLedgerError) as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_settlement_failed",
                    "Planner LLM call was dispatched but settlement failed",
                    details={"budget_error": str(exc), "operation_key": operation_key},
                )
            ) from exc
        except Exception as exc:
            raise await self._stop(
                BudgetExecutionStopped(
                    "llm_budget_settlement_unavailable",
                    "Planner LLM call was dispatched but settlement was unavailable",
                    details={"budget_error": str(exc), "operation_key": operation_key},
                )
            ) from exc
