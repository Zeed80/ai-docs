"""Versioned, non-secret usage evidence for recorded physical LLM attempts."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select

from app.db.models import WorkEvent, WorkOrder
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation

LLM_USAGE_EVENT_TYPE = "budget.llm_usage_receipt"
LLM_USAGE_SCHEMA = "llm_usage_receipt.v1"
LLM_USAGE_ACTOR = "budget-ledger"
_USAGE_EVENT_NAMESPACE = uuid.UUID("ea221aef-9ddf-4aa4-a891-7d4c98b689c4")

_OUTCOMES = frozenset(
    {
        "response_observed",
        "response_not_observed",
        "http_error",
        "response_body_invalid",
    }
)
_UNKNOWN_REASONS = frozenset(
    {
        "missing",
        "invalid_type",
        "negative",
        "response_not_observed",
        "http_error",
        "response_body_invalid",
    }
)
_TOTAL_UNKNOWN_REASONS = frozenset({"terminal_not_observed", "component_unknown"})
KNOWN_LLM_CALL_SETTLEMENT_DIGEST = hashlib.sha256(
    json.dumps(
        {"unknown": False, "actual_units": "1"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
).hexdigest()


@dataclass(frozen=True)
class TokenComponentEvidence:
    units: int | None
    unknown_reason: str | None


@dataclass(frozen=True)
class OllamaUsageEvidence:
    """Strict evidence captured from one Ollama HTTP response boundary."""

    outcome: str
    terminal_observed: bool
    input_tokens: TokenComponentEvidence
    output_tokens: TokenComponentEvidence


def usage_event_id(reservation_id: uuid.UUID) -> uuid.UUID:
    """Return one stable WorkEvent identity for one physical reservation."""
    return uuid.uuid5(_USAGE_EVENT_NAMESPACE, str(reservation_id))


def _component(raw: dict[str, Any], field: str) -> TokenComponentEvidence:
    if field not in raw:
        return TokenComponentEvidence(units=None, unknown_reason="missing")
    value = raw[field]
    if isinstance(value, bool) or not isinstance(value, int):
        return TokenComponentEvidence(units=None, unknown_reason="invalid_type")
    if value < 0:
        return TokenComponentEvidence(units=None, unknown_reason="negative")
    return TokenComponentEvidence(units=value, unknown_reason=None)


def ollama_usage_from_body(body: Any) -> OllamaUsageEvidence:
    """Parse only top-level Ollama counters; never coerce missing values to zero."""
    if not isinstance(body, dict):
        return unknown_ollama_usage("response_body_invalid")
    return OllamaUsageEvidence(
        outcome="response_observed",
        terminal_observed=body.get("done") is True,
        input_tokens=_component(body, "prompt_eval_count"),
        output_tokens=_component(body, "eval_count"),
    )


def unknown_ollama_usage(reason: str) -> OllamaUsageEvidence:
    """Build an explicit unknown receipt without carrying response/error text."""
    if reason not in _UNKNOWN_REASONS:
        raise ValueError("Unsupported Ollama usage unknown reason")
    outcome = reason if reason in _OUTCOMES else "response_not_observed"
    component = TokenComponentEvidence(units=None, unknown_reason=reason)
    return OllamaUsageEvidence(
        outcome=outcome,
        terminal_observed=False,
        input_tokens=component,
        output_tokens=component,
    )


def _canonical_component(component: TokenComponentEvidence) -> dict[str, Any]:
    if component.units is not None:
        if (
            isinstance(component.units, bool)
            or not isinstance(component.units, int)
            or component.units < 0
            or component.unknown_reason is not None
        ):
            raise ValueError("Known token evidence must be a non-negative integer")
        return {"status": "known", "units": component.units}
    if component.unknown_reason not in _UNKNOWN_REASONS:
        raise ValueError("Unknown token evidence requires a supported reason")
    return {"status": "unknown", "reason": component.unknown_reason}


def canonical_usage_payload(
    reservation: WorkBudgetReservation,
    evidence: OllamaUsageEvidence,
    *,
    owner_key: str,
) -> dict[str, Any]:
    """Bind canonical evidence to authoritative reservation fields."""
    if evidence.outcome not in _OUTCOMES or not isinstance(evidence.terminal_observed, bool):
        raise ValueError("Unsupported Ollama usage outcome")
    input_component = _canonical_component(evidence.input_tokens)
    output_component = _canonical_component(evidence.output_tokens)
    if evidence.outcome != "response_observed" and (
        evidence.terminal_observed
        or input_component["status"] == "known"
        or output_component["status"] == "known"
    ):
        raise ValueError("Unobserved Ollama responses cannot carry known usage")
    total_known = (
        evidence.terminal_observed
        and input_component["status"] == "known"
        and output_component["status"] == "known"
    )
    if total_known:
        total_component: dict[str, Any] = {
            "status": "known",
            "units": input_component["units"] + output_component["units"],
        }
    else:
        total_component = {
            "status": "unknown",
            "reason": (
                "terminal_not_observed" if not evidence.terminal_observed else "component_unknown"
            ),
        }
    return {
        "schema": LLM_USAGE_SCHEMA,
        "reservation_id": str(reservation.id),
        "ledger_id": str(reservation.ledger_id),
        "work_order_id": str(reservation.work_order_id),
        "owner_key": owner_key,
        "operation_key": reservation.operation_key,
        "request_digest": reservation.request_digest,
        "binding_digest": reservation.binding_digest,
        "provider": "ollama",
        "outcome": evidence.outcome,
        "terminal_observed": evidence.terminal_observed,
        "tokens": {
            "input": input_component,
            "output": output_component,
            "total": total_component,
        },
        "cost": {
            "status": "unknown",
            "currency": "USD",
            "reason": "tariff_unavailable",
            "tariff_version": None,
        },
    }


def valid_usage_receipt_payload(
    payload: Any,
    reservation: WorkBudgetReservation,
    *,
    owner_key: str,
) -> bool:
    if not isinstance(payload, dict):
        return False
    expected_identity = {
        "schema": LLM_USAGE_SCHEMA,
        "reservation_id": str(reservation.id),
        "ledger_id": str(reservation.ledger_id),
        "work_order_id": str(reservation.work_order_id),
        "owner_key": owner_key,
        "operation_key": reservation.operation_key,
        "request_digest": reservation.request_digest,
        "binding_digest": reservation.binding_digest,
        "provider": "ollama",
    }
    if set(payload) != {
        *expected_identity,
        "outcome",
        "terminal_observed",
        "tokens",
        "cost",
    } or any(payload.get(key) != value for key, value in expected_identity.items()):
        return False
    cost = payload.get("cost")
    if cost != {
        "status": "unknown",
        "currency": "USD",
        "reason": "tariff_unavailable",
        "tariff_version": None,
    }:
        return False
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict) or set(tokens) != {"input", "output", "total"}:
        return False
    parsed_components: dict[str, TokenComponentEvidence] = {}
    for key in ("input", "output"):
        component = tokens.get(key)
        if not isinstance(component, dict):
            return False
        if set(component) == {"status", "units"} and component.get("status") == "known":
            units = component.get("units")
            if isinstance(units, bool) or not isinstance(units, int) or units < 0:
                return False
            parsed_components[key] = TokenComponentEvidence(units=units, unknown_reason=None)
        elif (
            set(component) == {"status", "reason"}
            and component.get("status") == "unknown"
            and isinstance(component.get("reason"), str)
            and component.get("reason") in _UNKNOWN_REASONS
        ):
            parsed_components[key] = TokenComponentEvidence(
                units=None, unknown_reason=component["reason"]
            )
        else:
            return False
    total = tokens.get("total")
    if not isinstance(total, dict):
        return False
    if set(total) == {"status", "units"} and total.get("status") == "known":
        total_units = total.get("units")
        if (
            isinstance(total_units, bool)
            or not isinstance(total_units, int)
            or total_units < 0
            or parsed_components["input"].units is None
            or parsed_components["output"].units is None
            or total_units != parsed_components["input"].units + parsed_components["output"].units
        ):
            return False
    elif not (
        set(total) == {"status", "reason"}
        and total.get("status") == "unknown"
        and isinstance(total.get("reason"), str)
        and total.get("reason") in _TOTAL_UNKNOWN_REASONS
    ):
        return False
    try:
        evidence = OllamaUsageEvidence(
            outcome=payload.get("outcome"),
            terminal_observed=payload.get("terminal_observed"),
            input_tokens=parsed_components["input"],
            output_tokens=parsed_components["output"],
        )
        return canonical_usage_payload(reservation, evidence, owner_key=owner_key) == payload
    except (TypeError, ValueError):
        return False


def _valid_reservation_binding(reservation: WorkBudgetReservation) -> bool:
    request_digest = str(reservation.request_digest or "").lower()
    if len(request_digest) != 64 or any(c not in "0123456789abcdef" for c in request_digest):
        return False
    reserved = Decimal(reservation.reserved_units)
    reserved_text = format(reserved, "f")
    if "." in reserved_text:
        reserved_text = reserved_text.rstrip("0").rstrip(".")
    expected = hashlib.sha256(
        json.dumps(
            {
                "work_order_id": str(reservation.work_order_id),
                "operation_key": reservation.operation_key,
                "dimension": reservation.dimension,
                "reserved_units": reserved_text,
                "request_digest": request_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    return reservation.binding_digest == expected


async def read_recorded_llm_usage(
    session_factory,
    *,
    work_order_id: uuid.UUID,
    expected_owner_key: str,
) -> dict[str, Any]:
    """Aggregate only physical LLM reservations recorded on one shared ledger."""
    from app.domain.work_budget_ledger import BudgetBindingConflict

    async with session_factory() as db:
        async with db.begin():
            order = await db.get(WorkOrder, work_order_id, with_for_update=True)
            if (
                order is None
                or order.owner_key != expected_owner_key
                or order.budget_ledger_id is None
            ):
                raise BudgetBindingConflict("Usage summary WorkOrder binding is invalid")
            ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id, with_for_update=True)
            if ledger is None or ledger.owner_key != expected_owner_key:
                raise BudgetBindingConflict("Usage summary ledger owner binding is invalid")
            bound_orders = list(
                await db.scalars(select(WorkOrder).where(WorkOrder.budget_ledger_id == ledger.id))
            )
            if not bound_orders or any(row.owner_key != expected_owner_key for row in bound_orders):
                raise BudgetBindingConflict("Shared ledger contains a different owner")
            reservations = list(
                await db.scalars(
                    select(WorkBudgetReservation)
                    .where(
                        WorkBudgetReservation.ledger_id == ledger.id,
                        WorkBudgetReservation.dimension == "llm_calls",
                        WorkBudgetReservation.reserved_units > 0,
                    )
                    .order_by(WorkBudgetReservation.reserved_at, WorkBudgetReservation.id)
                )
            )
            event_ids = [usage_event_id(row.id) for row in reservations]
            events = (
                list(await db.scalars(select(WorkEvent).where(WorkEvent.id.in_(event_ids))))
                if event_ids
                else []
            )

    events_by_id = {event.id: event for event in events}
    known_input = 0
    known_output = 0
    known_lower_bound = 0
    exact_total = 0
    unknown_attempts = 0
    missing_receipts = 0
    invalid_receipts = 0
    reserved_attempts = 0
    known_total_attempts = 0
    unsupported_reservations = 0
    bound_order_ids = {row.id for row in bound_orders}

    for reservation in reservations:
        if (
            Decimal(reservation.reserved_units) != 1
            or reservation.work_order_id not in bound_order_ids
            or not _valid_reservation_binding(reservation)
        ):
            unsupported_reservations += 1
            unknown_attempts += 1
            continue
        event = events_by_id.get(usage_event_id(reservation.id))
        if reservation.state == "reserved":
            reserved_attempts += 1
        if event is None:
            missing_receipts += 1
            unknown_attempts += 1
            continue
        if (
            event.work_order_id != reservation.work_order_id
            or event.event_type != LLM_USAGE_EVENT_TYPE
            or event.actor != LLM_USAGE_ACTOR
            or not valid_usage_receipt_payload(
                event.payload, reservation, owner_key=expected_owner_key
            )
        ):
            invalid_receipts += 1
            unknown_attempts += 1
            continue
        if (
            reservation.state != "charged"
            or reservation.actual_unknown is not False
            or Decimal(reservation.actual_units or 0) != 1
            or reservation.settlement_digest != KNOWN_LLM_CALL_SETTLEMENT_DIGEST
            or reservation.settled_at is None
        ):
            invalid_receipts += 1
            unknown_attempts += 1
            continue
        tokens = event.payload["tokens"]
        input_units = tokens["input"].get("units") if tokens["input"]["status"] == "known" else None
        output_units = (
            tokens["output"].get("units") if tokens["output"]["status"] == "known" else None
        )
        if input_units is not None:
            known_input += input_units
            known_lower_bound += input_units
        if output_units is not None:
            known_output += output_units
            known_lower_bound += output_units
        if tokens["total"]["status"] != "known":
            unknown_attempts += 1
            continue
        known_total_attempts += 1
        exact_total += tokens["total"]["units"]

    attempts = len(reservations)
    token_status: Literal["known", "partial", "unknown"]
    if unknown_attempts == 0:
        token_status = "known"
    elif known_lower_bound > 0 or known_total_attempts > 0:
        token_status = "partial"
    else:
        token_status = "unknown"
    return {
        "ledger_id": str(ledger.id),
        "root_work_order_id": str(ledger.root_work_order_id),
        "owner_key": expected_owner_key,
        "coverage": {
            "scope": "recorded_physical_llm_reservations",
            "complete_for_scope": unknown_attempts == 0,
            "system_wide": "not_claimed",
        },
        "attempts": {
            "recorded": attempts,
            "known_total": known_total_attempts,
            "unknown": unknown_attempts,
            "missing_receipts": missing_receipts,
            "invalid_receipts": invalid_receipts,
            "reserved": reserved_attempts,
            "unsupported_reservations": unsupported_reservations,
        },
        "tokens": {
            "status": token_status,
            "input_known_lower_bound": known_input,
            "output_known_lower_bound": known_output,
            "known_lower_bound": known_lower_bound,
            "total": exact_total if unknown_attempts == 0 else None,
        },
        "cost": {
            "status": "not_observed" if attempts == 0 else "unknown",
            "currency": "USD",
            "total": None,
            "reason": None if attempts == 0 else "tariff_unavailable",
        },
    }
