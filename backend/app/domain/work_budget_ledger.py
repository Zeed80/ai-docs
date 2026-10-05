"""Atomic shared budget reservations for durable WorkOrders.

The functions in this module own their transaction and return only after it is
committed.  Runtime callers must reserve before dispatching an external effect.
E21.1 deliberately does not connect those callers yet.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from sqlalchemy import case, func, select

from app.db.models import WorkEvent, WorkOrder, WorkStep, WorkStepAttempt, WorkToolCall
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation

DEFAULT_ACTIVE_SECONDS = Decimal("7200")
DEFAULT_TOOL_ATTEMPTS = Decimal("200")
DEFAULT_LLM_CALLS = Decimal("50")
DEFAULT_REPLANS = Decimal("3")

DIMENSION_LIMITS = {
    "active_seconds": "max_active_seconds",
    "tool_attempts": "max_tool_attempts",
    "llm_calls": "max_llm_calls",
    "replans": "max_replans",
    "tokens": "max_tokens",
    "cost_usd": "max_cost_usd",
}


class BudgetLedgerError(ValueError):
    pass


class BudgetBindingConflict(BudgetLedgerError):
    pass


class LegacyBudgetBaselineRequired(BudgetLedgerError):
    pass


class BudgetReservationConflict(BudgetLedgerError):
    pass


class BudgetExceeded(BudgetLedgerError):
    pass


def _decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise BudgetLedgerError(f"{field} must be a finite non-negative number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BudgetLedgerError(f"{field} must be a finite non-negative number") from None
    if not result.is_finite() or result < 0:
        raise BudgetLedgerError(f"{field} must be a finite non-negative number")
    # All persisted values use NUMERIC(30, 8). Strip only insignificant
    # fractional zeroes while inspecting the exact tuple; Decimal.normalize()
    # is deliberately avoided because it obeys the 28-digit ambient context.
    digits = list(result.as_tuple().digits)
    exponent = result.as_tuple().exponent
    while digits and digits[-1] == 0 and exponent < 0:
        digits.pop()
        exponent += 1
    fractional_digits = max(0, -exponent)
    integer_digits = max(0, len(digits) + exponent)
    if fractional_digits > 8 or integer_digits > 22:
        raise BudgetLedgerError(f"{field} exceeds NUMERIC(30, 8) precision")
    return result


def _count(value: Any, *, field: str) -> Decimal:
    result = _decimal(value, field=field)
    if result != result.to_integral_value():
        raise BudgetLedgerError(f"{field} must be an integer")
    return result


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    exact = format(value, "f")
    if "." in exact:
        exact = exact.rstrip("0").rstrip(".")
    return exact


def _exact_sum(*values: Decimal) -> Decimal:
    # NUMERIC(30, 8) operands can require 31 digits during an over-limit sum.
    # Stay independent of the process-wide Decimal context (normally 28).
    with localcontext() as context:
        context.prec = 64
        return sum(values, Decimal(0))


def _exact_settlement_total(current: Decimal, reserved: Decimal, actual: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 64
        return current - reserved + actual


def _request_digest(value: str) -> str:
    normalized = str(value or "").lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise BudgetLedgerError("request_digest must be a lowercase SHA-256 hex digest")
    return normalized


def _optional_decimal(value: Any, *, field: str) -> Decimal | None:
    return None if value is None else _decimal(value, field=field)


def normalize_limits(budgets: dict[str, Any] | None) -> dict[str, Decimal | None]:
    """Normalize only declared E21 units; wall-clock remains a legacy guard."""
    values = budgets or {}
    return {
        "max_active_seconds": _decimal(
            values.get("max_active_seconds", DEFAULT_ACTIVE_SECONDS), field="max_active_seconds"
        ),
        "max_tool_attempts": _count(
            values.get("max_tool_attempts", values.get("max_tool_calls", DEFAULT_TOOL_ATTEMPTS)),
            field="max_tool_attempts",
        ),
        "max_llm_calls": _count(
            values.get("max_llm_calls", DEFAULT_LLM_CALLS), field="max_llm_calls"
        ),
        "max_replans": _count(values.get("max_replans", DEFAULT_REPLANS), field="max_replans"),
        "max_tokens": (
            None
            if values.get("token_budget") is None
            else _count(values.get("token_budget"), field="token_budget")
        ),
        "max_cost_usd": _optional_decimal(values.get("max_cost_usd"), field="max_cost_usd"),
    }


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


async def _unlocked_ancestry(db, work_order_id: uuid.UUID) -> list[WorkOrder]:
    ancestry: list[WorkOrder] = []
    seen: set[uuid.UUID] = set()
    current_id: uuid.UUID | None = work_order_id
    while current_id is not None:
        if current_id in seen:
            raise BudgetBindingConflict("WorkOrder parent lineage contains a cycle")
        seen.add(current_id)
        current = await db.get(WorkOrder, current_id)
        if current is None:
            raise BudgetBindingConflict("WorkOrder parent lineage is incomplete")
        ancestry.append(current)
        current_id = current.parent_id
    return list(reversed(ancestry))


async def _lock_lineage(db, work_order_id: uuid.UUID) -> list[WorkOrder]:
    """Lock root first, then all descendants in deterministic breadth-first order."""
    initial = await _unlocked_ancestry(db, work_order_id)
    root_id = initial[0].id
    expected_owner = initial[0].owner_key
    result: list[WorkOrder] = []
    frontier = [root_id]
    seen: set[uuid.UUID] = set()
    while frontier:
        level_ids = sorted(frontier, key=str)
        rows = list(
            (
                await db.execute(
                    select(WorkOrder)
                    .where(WorkOrder.id.in_(level_ids))
                    .order_by(WorkOrder.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        if len(rows) != len(level_ids):
            raise BudgetBindingConflict("WorkOrder lineage changed while it was being locked")
        for row in rows:
            if row.id in seen:
                raise BudgetBindingConflict("WorkOrder parent lineage contains a cycle")
            if row.owner_key != expected_owner:
                raise BudgetBindingConflict("Child WorkOrder owner differs from budget root owner")
            seen.add(row.id)
            result.append(row)
        children = list(
            (
                await db.execute(
                    select(WorkOrder.id)
                    .where(WorkOrder.parent_id.in_(level_ids))
                    .order_by(WorkOrder.id)
                )
            ).scalars()
        )
        frontier = [child_id for child_id in children if child_id not in seen]
    if work_order_id not in seen:
        raise BudgetBindingConflict("Requested WorkOrder was reparented during budget binding")
    # Re-resolve the requested path under the locks: parent_id is authoritative,
    # never client metadata or a caller-provided root id.
    by_id = {row.id: row for row in result}
    cursor = by_id[work_order_id]
    path_seen: set[uuid.UUID] = set()
    while cursor.parent_id is not None:
        if cursor.id in path_seen or cursor.parent_id not in by_id:
            raise BudgetBindingConflict("WorkOrder parent lineage changed during budget binding")
        path_seen.add(cursor.id)
        cursor = by_id[cursor.parent_id]
    if cursor.id != root_id:
        raise BudgetBindingConflict("WorkOrder budget root changed during budget binding")
    return result


async def _has_historic_usage(db, order_ids: list[uuid.UUID], orders: list[WorkOrder]) -> bool:
    attempt_exists = await db.scalar(
        select(WorkStepAttempt.id)
        .join(WorkStep, WorkStep.id == WorkStepAttempt.step_id)
        .where(WorkStep.work_order_id.in_(order_ids))
        .limit(1)
    )
    call_exists = await db.scalar(
        select(WorkToolCall.id).where(WorkToolCall.work_order_id.in_(order_ids)).limit(1)
    )
    durable_call_exists = await db.scalar(
        select(WorkEvent.id)
        .where(
            WorkEvent.work_order_id.in_(order_ids),
            WorkEvent.event_type == "chat.tool_call",
        )
        .limit(1)
    )
    return (
        attempt_exists is not None
        or call_exists is not None
        or durable_call_exists is not None
        or any(
            row.plan_revision > 0
            or row.started_at is not None
            or row.completed_at is not None
            or row.canceled_at is not None
            or row.status
            not in {
                "received",
                "scoping",
                "planning",
                "ready",
            }
            for row in orders
        )
    )


def _reject_tighter_descendant_limits(
    root: WorkOrder, descendants: list[WorkOrder], root_limits: dict[str, Decimal | None]
) -> None:
    key_to_limit = {
        "max_active_seconds": "max_active_seconds",
        "max_tool_attempts": "max_tool_attempts",
        "max_tool_calls": "max_tool_attempts",
        "max_llm_calls": "max_llm_calls",
        "max_replans": "max_replans",
        "token_budget": "max_tokens",
        "max_cost_usd": "max_cost_usd",
    }
    for child in descendants:
        if child.id == root.id:
            continue
        for key, limit_name in key_to_limit.items():
            if key not in (child.budgets or {}):
                continue
            child_limit = _decimal(child.budgets[key], field=f"child.{key}")
            root_limit = root_limits[limit_name]
            if root_limit is None or child_limit < root_limit:
                raise BudgetBindingConflict(
                    f"Child WorkOrder has tighter {key}; reconcile it before shared-ledger binding"
                )


def _root_budgets_with_mode_defaults(root: WorkOrder) -> dict[str, Any]:
    """Root budgets plus the per-mode replan default the runtime already uses.

    Exploratory work deliberately gets a generous replan allowance (see
    work_orders._DEFAULT_MAX_REPLANS_EXPLORATORY); the generic ledger default
    of 3 would silently cut it tenfold once replans are charged here.
    """
    from app.domain.work_orders import _DEFAULT_MAX_REPLANS_EXPLORATORY, is_exploratory

    budgets = dict(root.budgets or {})
    if "max_replans" not in budgets and is_exploratory(root):
        budgets["max_replans"] = _DEFAULT_MAX_REPLANS_EXPLORATORY
    return budgets


async def initialize_budget_ledger(db, work_order_id: uuid.UUID) -> WorkBudgetLedger:
    """Create/bind a fresh lineage inside the caller's current transaction.

    Intake uses this before it creates a plan so the WorkOrder, durable run,
    plan and ledger have one rollback boundary. Historic work is deliberately
    rejected instead of receiving an invented zero-usage baseline.
    """
    orders = await _lock_lineage(db, work_order_id)
    root = next(row for row in orders if row.parent_id is None)
    order_ids = [row.id for row in orders]
    existing_ids = {row.budget_ledger_id for row in orders if row.budget_ledger_id}
    if len(existing_ids) > 1:
        raise BudgetBindingConflict("WorkOrder lineage is bound to multiple budget ledgers")
    ledger = await db.get(WorkBudgetLedger, next(iter(existing_ids))) if existing_ids else None
    if ledger is not None and (
        ledger.root_work_order_id != root.id or ledger.owner_key != root.owner_key
    ):
        raise BudgetBindingConflict("Existing budget ledger does not match server lineage")
    if ledger is None and existing_ids:
        raise BudgetBindingConflict("WorkOrder references a missing budget ledger")
    unbound = [row for row in orders if row.budget_ledger_id is None]
    if ledger is not None and not unbound:
        return ledger
    if await _has_historic_usage(db, order_ids, orders):
        raise LegacyBudgetBaselineRequired(
            "Historic attempts, calls, or replans require an explicit baseline migration"
        )
    limits = normalize_limits(_root_budgets_with_mode_defaults(root))
    _reject_tighter_descendant_limits(root, orders, limits)
    if ledger is None:
        ledger = WorkBudgetLedger(
            root_work_order_id=root.id,
            owner_key=root.owner_key,
            **limits,
        )
        db.add(ledger)
        await db.flush()
    for row in orders:
        if row.budget_ledger_id not in {None, ledger.id}:
            raise BudgetBindingConflict("WorkOrder ledger binding is immutable")
        row.budget_ledger_id = ledger.id
    await db.flush()
    return ledger


async def bind_budget_ledger(session_factory, work_order_id: uuid.UUID) -> WorkBudgetLedger:
    """Create/bind a fresh lineage in one independently committed transaction."""
    async with session_factory() as db:
        async with db.begin():
            return await initialize_budget_ledger(db, work_order_id)


async def _usage(db, ledger_id: uuid.UUID, dimension: str) -> Decimal:
    amount = case(
        (WorkBudgetReservation.state == "charged", WorkBudgetReservation.actual_units),
        else_=WorkBudgetReservation.reserved_units,
    )
    value = await db.scalar(
        select(func.coalesce(func.sum(amount), 0)).where(
            WorkBudgetReservation.ledger_id == ledger_id,
            WorkBudgetReservation.dimension == dimension,
        )
    )
    return Decimal(value or 0)


async def _reserve_budget(
    session_factory,
    *,
    work_order_id: uuid.UUID,
    operation_key: str,
    dimension: str,
    units: Any,
    request_digest: str,
) -> tuple[WorkBudgetReservation, bool]:
    """Atomically reserve one dimension and commit before returning."""
    if dimension not in DIMENSION_LIMITS:
        raise BudgetLedgerError(f"Unsupported budget dimension: {dimension}")
    if not operation_key or len(operation_key) > 255:
        raise BudgetLedgerError("operation_key must contain 1..255 characters")
    reserved = (
        _count(units, field="units")
        if dimension in {"tool_attempts", "llm_calls", "replans", "tokens"}
        else _decimal(units, field="units")
    )
    request_fingerprint = _request_digest(request_digest)
    binding_digest = _digest(
        {
            "work_order_id": str(work_order_id),
            "operation_key": operation_key,
            "dimension": dimension,
            "reserved_units": _canonical_decimal(reserved),
            "request_digest": request_fingerprint,
        }
    )
    exceeded: dict[str, Any] | None = None
    async with session_factory() as db:
        async with db.begin():
            order = await db.get(WorkOrder, work_order_id)
            if order is None or order.budget_ledger_id is None:
                raise BudgetBindingConflict("WorkOrder has no initialized budget ledger")
            ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id, with_for_update=True)
            if ledger is None or ledger.owner_key != order.owner_key:
                raise BudgetBindingConflict("WorkOrder budget binding is invalid")
            existing = await db.scalar(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.ledger_id == ledger.id,
                    WorkBudgetReservation.operation_key == operation_key,
                )
            )
            if existing is not None:
                if existing.binding_digest != binding_digest:
                    raise BudgetReservationConflict(
                        "operation_key is already bound to different budget units"
                    )
                return existing, False
            if dimension == "cost_usd":
                unknown_cost = await db.scalar(
                    select(WorkBudgetReservation.id)
                    .where(
                        WorkBudgetReservation.ledger_id == ledger.id,
                        WorkBudgetReservation.dimension == "cost_usd",
                        WorkBudgetReservation.state == "unknown",
                    )
                    .limit(1)
                )
                if unknown_cost is not None:
                    exceeded = {
                        "code": "cost_usage_unknown",
                        "dimension": "cost_usd",
                        "requested": _canonical_decimal(reserved),
                    }
                    ledger.blocker = exceeded
            current = await _usage(db, ledger.id, dimension)
            limit = getattr(ledger, DIMENSION_LIMITS[dimension])
            requested_total = _exact_sum(current, reserved)
            if exceeded is None and limit is not None and requested_total > Decimal(limit):
                exceeded = {
                    "code": "budget_reservation_exceeded",
                    "dimension": dimension,
                    "limit": str(limit),
                    "used_or_reserved": str(current),
                    "requested": str(reserved),
                }
                ledger.blocker = exceeded
            if exceeded is None:
                reservation = WorkBudgetReservation(
                    ledger_id=ledger.id,
                    work_order_id=order.id,
                    operation_key=operation_key,
                    dimension=dimension,
                    reserved_units=reserved,
                    request_digest=request_fingerprint,
                    binding_digest=binding_digest,
                    state="reserved",
                )
                db.add(reservation)
                await db.flush()
        if exceeded is not None:
            raise BudgetExceeded(json.dumps(exceeded, sort_keys=True))
        return reservation, True


async def reserve_budget(
    session_factory,
    *,
    work_order_id: uuid.UUID,
    operation_key: str,
    dimension: str,
    units: Any,
    request_digest: str,
) -> WorkBudgetReservation:
    """Idempotently reserve a dimension for non-dispatch callers.

    Existing callers retain the E21.1 API. Effect dispatchers must use
    ``reserve_budget_for_dispatch`` and require ``created`` to be true.
    """
    reservation, _ = await _reserve_budget(
        session_factory,
        work_order_id=work_order_id,
        operation_key=operation_key,
        dimension=dimension,
        units=units,
        request_digest=request_digest,
    )
    return reservation


async def reserve_budget_for_dispatch(
    session_factory,
    *,
    work_order_id: uuid.UUID,
    operation_key: str,
    dimension: str,
    units: Any,
    request_digest: str,
) -> tuple[WorkBudgetReservation, bool]:
    """Reserve atomically and report whether this transaction created it.

    A matching pre-existing reservation is evidence of an earlier dispatch
    boundary, never permission to repeat the external effect.
    """
    return await _reserve_budget(
        session_factory,
        work_order_id=work_order_id,
        operation_key=operation_key,
        dimension=dimension,
        units=units,
        request_digest=request_digest,
    )


async def settle_budget(
    session_factory,
    *,
    work_order_id: uuid.UUID,
    operation_key: str,
    actual_units: Any | None = None,
    unknown: bool = False,
) -> WorkBudgetReservation:
    """Idempotently charge known usage, or retain the full reserve as unknown."""
    if unknown == (actual_units is not None):
        raise BudgetLedgerError("settlement must be exactly one of known actual_units or unknown")
    actual = None
    if not unknown:
        # The reservation is loaded below, but every integer-only dimension is
        # encoded in the operation binding. Validate after loading it as well.
        actual = _decimal(actual_units, field="actual_units")
    settlement_digest = _digest(
        {
            "unknown": unknown,
            "actual_units": None if unknown else _canonical_decimal(actual),
        }
    )
    async with session_factory() as db:
        async with db.begin():
            order = await db.get(WorkOrder, work_order_id)
            if order is None or order.budget_ledger_id is None:
                raise BudgetBindingConflict("WorkOrder has no initialized budget ledger")
            ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id, with_for_update=True)
            if ledger is None or ledger.owner_key != order.owner_key:
                raise BudgetBindingConflict("WorkOrder budget binding is invalid")
            reservation = await db.scalar(
                select(WorkBudgetReservation)
                .where(
                    WorkBudgetReservation.ledger_id == order.budget_ledger_id,
                    WorkBudgetReservation.operation_key == operation_key,
                )
                .with_for_update()
            )
            if reservation is None or reservation.work_order_id != order.id:
                raise BudgetReservationConflict("Budget reservation does not match WorkOrder")
            if (
                actual is not None
                and reservation.dimension in {"tool_attempts", "llm_calls", "replans", "tokens"}
                and actual != actual.to_integral_value()
            ):
                raise BudgetLedgerError("actual_units must be an integer for this dimension")
            if reservation.state != "reserved":
                if reservation.settlement_digest != settlement_digest:
                    raise BudgetReservationConflict(
                        "operation_key is already settled with a different actual payload"
                    )
                return reservation
            current = await _usage(db, ledger.id, reservation.dimension)
            blocker = None
            if unknown:
                reservation.state = "unknown"
                reservation.actual_unknown = True
                if reservation.dimension == "cost_usd":
                    blocker = {
                        "code": "cost_usage_unknown",
                        "operation_key": operation_key,
                    }
            else:
                reservation.state = "charged"
                reservation.actual_units = actual
                if actual > reservation.reserved_units:
                    blocker = {
                        "code": "budget_actual_overrun",
                        "dimension": reservation.dimension,
                        "reserved": str(reservation.reserved_units),
                        "actual": str(actual),
                    }
                limit = getattr(ledger, DIMENSION_LIMITS[reservation.dimension])
                actual_total = _exact_settlement_total(current, reservation.reserved_units, actual)
                if limit is not None and actual_total > Decimal(limit):
                    blocker = {
                        "code": "budget_charge_exceeded",
                        "dimension": reservation.dimension,
                        "limit": str(limit),
                        "charged_or_reserved": str(actual_total),
                    }
            reservation.settlement_digest = settlement_digest
            reservation.settled_at = datetime.now(UTC)
            reservation.blocker = blocker
            if blocker is not None:
                ledger.blocker = blocker
        return reservation


async def settle_llm_call_with_usage_receipt(
    session_factory,
    *,
    work_order_id: uuid.UUID,
    operation_key: str,
    expected_owner_key: str,
    evidence,
) -> tuple[WorkBudgetReservation, WorkEvent]:
    """Atomically settle one physical LLM call and append its usage receipt.

    Lock order is WorkOrder -> ledger -> reservation -> event sequence.  This
    deliberately differs from calling ``settle_budget`` followed by
    ``append_event``: that split would permit a charged call with no receipt.
    """
    from app.domain.work_budget_usage import (
        LLM_USAGE_ACTOR,
        LLM_USAGE_EVENT_TYPE,
        canonical_usage_payload,
        usage_event_id,
        valid_usage_receipt_payload,
    )

    actual = Decimal(1)
    settlement_digest = _digest({"unknown": False, "actual_units": _canonical_decimal(actual)})
    async with session_factory() as db:
        async with db.begin():
            order = await db.get(WorkOrder, work_order_id, with_for_update=True)
            if (
                order is None
                or order.owner_key != expected_owner_key
                or order.budget_ledger_id is None
            ):
                raise BudgetBindingConflict("WorkOrder has no initialized budget ledger")
            ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id, with_for_update=True)
            if ledger is None or ledger.owner_key != expected_owner_key:
                raise BudgetBindingConflict("WorkOrder budget binding is invalid")
            reservation = await db.scalar(
                select(WorkBudgetReservation)
                .where(
                    WorkBudgetReservation.ledger_id == ledger.id,
                    WorkBudgetReservation.operation_key == operation_key,
                )
                .with_for_update()
            )
            if (
                reservation is None
                or reservation.work_order_id != order.id
                or reservation.dimension != "llm_calls"
                or Decimal(reservation.reserved_units) != 1
            ):
                raise BudgetReservationConflict(
                    "Usage receipt requires the exact physical LLM reservation"
                )
            expected_binding = _digest(
                {
                    "work_order_id": str(order.id),
                    "operation_key": reservation.operation_key,
                    "dimension": reservation.dimension,
                    "reserved_units": _canonical_decimal(Decimal(reservation.reserved_units)),
                    "request_digest": _request_digest(reservation.request_digest),
                }
            )
            if reservation.binding_digest != expected_binding:
                raise BudgetReservationConflict("LLM reservation binding digest is invalid")

            receipt_payload = canonical_usage_payload(
                reservation, evidence, owner_key=expected_owner_key
            )
            receipt_id = usage_event_id(reservation.id)
            existing_event = await db.get(WorkEvent, receipt_id)
            if reservation.state == "charged":
                if (
                    reservation.settlement_digest != settlement_digest
                    or reservation.actual_units is None
                    or Decimal(reservation.actual_units) != 1
                    or reservation.actual_unknown is not False
                    or reservation.settled_at is None
                    or existing_event is None
                ):
                    raise BudgetReservationConflict(
                        "Charged LLM reservation has no matching atomic usage receipt"
                    )
                if (
                    existing_event.work_order_id != order.id
                    or existing_event.event_type != LLM_USAGE_EVENT_TYPE
                    or existing_event.actor != LLM_USAGE_ACTOR
                    or not valid_usage_receipt_payload(
                        existing_event.payload,
                        reservation,
                        owner_key=expected_owner_key,
                    )
                    or existing_event.payload != receipt_payload
                ):
                    raise BudgetReservationConflict(
                        "LLM usage receipt is already recorded with different evidence"
                    )
                return reservation, existing_event
            if reservation.state != "reserved":
                raise BudgetReservationConflict("Unknown LLM settlement cannot be reopened")
            if existing_event is not None:
                raise BudgetReservationConflict(
                    "Reserved LLM call already has an inconsistent usage receipt"
                )

            reservation.state = "charged"
            reservation.actual_units = actual
            reservation.actual_unknown = False
            reservation.settlement_digest = settlement_digest
            reservation.settled_at = datetime.now(UTC)
            current = await _usage(db, ledger.id, reservation.dimension)
            actual_total = _exact_settlement_total(
                current, Decimal(reservation.reserved_units), actual
            )
            limit = getattr(ledger, DIMENSION_LIMITS[reservation.dimension])
            blocker = None
            if limit is not None and actual_total > Decimal(limit):
                blocker = {
                    "code": "budget_charge_exceeded",
                    "dimension": reservation.dimension,
                    "limit": str(limit),
                    "charged_or_reserved": str(actual_total),
                }
                ledger.blocker = blocker
            reservation.blocker = blocker
            sequence = (
                int(
                    await db.scalar(
                        select(func.coalesce(func.max(WorkEvent.sequence), 0)).where(
                            WorkEvent.work_order_id == order.id
                        )
                    )
                    or 0
                )
                + 1
            )
            event = WorkEvent(
                id=receipt_id,
                work_order_id=order.id,
                sequence=sequence,
                event_type=LLM_USAGE_EVENT_TYPE,
                actor=LLM_USAGE_ACTOR,
                payload=receipt_payload,
            )
            db.add(event)
            await db.flush()
        return reservation, event


async def charge_replan_in_transaction(db, order: WorkOrder) -> bool:
    """Charge one replan to the order's shared ledger in the caller's transaction.

    Replans used to be limited per WorkOrder only, so every child of a
    decomposed task got its own full allowance. The charge is idempotent per
    (order, plan revision) and rolls back with the caller's transition.
    Returns False when the shared allowance is exhausted; that blocks only
    this order and does not set a ledger-wide blocker. An order without a
    ledger keeps the legacy per-order rule (durable execution refuses such
    work anyway).
    """
    if order.budget_ledger_id is None:
        return True
    ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id, with_for_update=True)
    if ledger is None or ledger.owner_key != order.owner_key:
        raise BudgetBindingConflict("WorkOrder budget binding is invalid")
    operation_key = f"replan:{order.id}:from-r{order.plan_revision}"
    existing = await db.scalar(
        select(WorkBudgetReservation).where(
            WorkBudgetReservation.ledger_id == ledger.id,
            WorkBudgetReservation.operation_key == operation_key,
        )
    )
    if existing is not None:
        return True
    current = await _usage(db, ledger.id, "replans")
    if ledger.max_replans is not None and _exact_sum(current, Decimal(1)) > Decimal(
        ledger.max_replans
    ):
        return False
    units = Decimal(1)
    request_digest = _digest(
        {"work_order_id": str(order.id), "from_plan_revision": order.plan_revision}
    )
    db.add(
        WorkBudgetReservation(
            ledger_id=ledger.id,
            work_order_id=order.id,
            operation_key=operation_key,
            dimension="replans",
            reserved_units=units,
            request_digest=request_digest,
            binding_digest=_digest(
                {
                    "work_order_id": str(order.id),
                    "operation_key": operation_key,
                    "dimension": "replans",
                    "reserved_units": _canonical_decimal(units),
                    "request_digest": request_digest,
                }
            ),
            state="charged",
            actual_units=units,
            actual_unknown=False,
            settlement_digest=_digest(
                {"unknown": False, "actual_units": _canonical_decimal(units)}
            ),
            settled_at=datetime.now(UTC),
        )
    )
    await db.flush()
    return True
