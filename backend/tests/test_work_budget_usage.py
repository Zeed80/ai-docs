"""Atomic, versioned usage receipts for recorded physical LLM reservations."""

import asyncio
import hashlib
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import WorkEvent, WorkOrder
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.work_budget_ledger import (
    BudgetBindingConflict,
    BudgetReservationConflict,
    initialize_budget_ledger,
    reserve_budget_for_dispatch,
    settle_budget,
    settle_llm_call_with_usage_receipt,
)
from app.domain.work_budget_usage import (
    LLM_USAGE_EVENT_TYPE,
    ollama_usage_from_body,
    read_recorded_llm_usage,
    unknown_ollama_usage,
    usage_event_id,
)
from app.domain.work_orders import create_work_order

REQUEST_DIGEST = hashlib.sha256(b"usage receipt request").hexdigest()


def _factory(test_engine):
    return async_sessionmaker(test_engine, expire_on_commit=False)


async def _fresh_order(factory, *, budgets=None, parent_id=None, initialize=True):
    async with factory() as db:
        order = await create_work_order(
            db,
            owner_key="usage-owner",
            objective="Record exact provider usage",
            budgets=budgets,
            parent_id=parent_id,
        )
        if initialize:
            await initialize_budget_ledger(db, order.id)
        await db.commit()
        return order.id


async def _lineage(factory):
    async with factory() as db:
        root = await create_work_order(
            db,
            owner_key="usage-owner",
            objective="Root usage",
            budgets={"max_llm_calls": 20},
        )
        child = await create_work_order(
            db,
            owner_key="usage-owner",
            objective="Child usage",
            parent_id=root.id,
        )
        ledger = await initialize_budget_ledger(db, child.id)
        await db.commit()
        return root.id, child.id, ledger.id


async def _reserve(factory, order_id, key, *, units=1):
    row, created = await reserve_budget_for_dispatch(
        factory,
        work_order_id=order_id,
        operation_key=key,
        dimension="llm_calls",
        units=units,
        request_digest=REQUEST_DIGEST,
    )
    assert created
    return row


def _known(input_tokens=3, output_tokens=5, *, done=True):
    return ollama_usage_from_body(
        {
            "done": done,
            "prompt_eval_count": input_tokens,
            "eval_count": output_tokens,
            "message": {"content": "must not enter receipt"},
        }
    )


@pytest.mark.parametrize(
    ("body", "input_status", "output_status", "terminal"),
    [
        ({"done": True, "prompt_eval_count": 0, "eval_count": 0}, "known", "known", True),
        ({"done": True, "prompt_eval_count": 7}, "known", "unknown", True),
        ({"done": True, "prompt_eval_count": True, "eval_count": 2}, "unknown", "known", True),
        ({"done": True, "prompt_eval_count": "3", "eval_count": -1}, "unknown", "unknown", True),
        ({"done": 1, "prompt_eval_count": 3, "eval_count": 2}, "known", "known", False),
    ],
)
def test_ollama_parser_is_strict_and_preserves_known_components(
    body, input_status, output_status, terminal
):
    evidence = ollama_usage_from_body(body)
    assert (evidence.input_tokens.units is not None) == (input_status == "known")
    assert (evidence.output_tokens.units is not None) == (output_status == "known")
    assert evidence.terminal_observed is terminal


@pytest.mark.asyncio
async def test_atomic_receipt_is_bound_idempotent_and_contains_no_raw_body(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory, budgets={"max_llm_calls": 3})
    reservation = await _reserve(factory, order_id, "llm:known")
    evidence = _known()

    first, first_event = await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=order_id,
        operation_key=reservation.operation_key,
        expected_owner_key="usage-owner",
        evidence=evidence,
    )
    second, second_event = await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=order_id,
        operation_key=reservation.operation_key,
        expected_owner_key="usage-owner",
        evidence=evidence,
    )

    assert first.id == second.id == reservation.id
    assert first_event.id == second_event.id == usage_event_id(reservation.id)
    assert first.state == "charged"
    assert first.actual_units == 1
    assert first_event.payload["tokens"]["total"] == {"status": "known", "units": 8}
    assert first_event.payload["cost"]["status"] == "unknown"
    assert "message" not in str(first_event.payload)
    async with factory() as db:
        events = list(
            await db.scalars(
                select(WorkEvent).where(
                    WorkEvent.work_order_id == order_id,
                    WorkEvent.event_type == LLM_USAGE_EVENT_TYPE,
                )
            )
        )
    assert len(events) == 1


@pytest.mark.asyncio
async def test_concurrent_same_receipt_creates_one_event(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    reservation = await _reserve(factory, order_id, "llm:concurrent")

    results = await asyncio.gather(
        *(
            settle_llm_call_with_usage_receipt(
                factory,
                work_order_id=order_id,
                operation_key=reservation.operation_key,
                expected_owner_key="usage-owner",
                evidence=_known(),
            )
            for _ in range(2)
        )
    )
    assert results[0][1].id == results[1][1].id
    async with factory() as db:
        assert await db.get(WorkEvent, usage_event_id(reservation.id)) is not None


@pytest.mark.asyncio
async def test_changed_duplicate_evidence_conflicts(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    reservation = await _reserve(factory, order_id, "llm:conflict")
    await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=order_id,
        operation_key=reservation.operation_key,
        expected_owner_key="usage-owner",
        evidence=_known(),
    )

    with pytest.raises(BudgetReservationConflict):
        await settle_llm_call_with_usage_receipt(
            factory,
            work_order_id=order_id,
            operation_key=reservation.operation_key,
            expected_owner_key="usage-owner",
            evidence=_known(output_tokens=6),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actual_units", Decimal(0)),
        ("actual_unknown", True),
        ("settled_at", None),
    ],
)
async def test_idempotent_receipt_rejects_inconsistent_charged_state(test_engine, field, value):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    reservation = await _reserve(factory, order_id, f"llm:idempotent-{field}")
    _, event = await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=order_id,
        operation_key=reservation.operation_key,
        expected_owner_key="usage-owner",
        evidence=_known(),
    )
    original_payload = event.payload
    async with factory() as db:
        persisted = await db.get(WorkBudgetReservation, reservation.id)
        setattr(persisted, field, value)
        await db.commit()

    with pytest.raises(BudgetReservationConflict):
        await settle_llm_call_with_usage_receipt(
            factory,
            work_order_id=order_id,
            operation_key=reservation.operation_key,
            expected_owner_key="usage-owner",
            evidence=_known(),
        )
    async with factory() as db:
        persisted = await db.get(WorkBudgetReservation, reservation.id)
        assert getattr(persisted, field) == value
        persisted_event = await db.get(WorkEvent, event.id)
        assert persisted_event.payload == original_payload


@pytest.mark.asyncio
async def test_receipt_owner_binding_failure_does_not_charge(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    reservation = await _reserve(factory, order_id, "llm:owner-change")
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id)
        order.owner_key = "replacement-owner"
        ledger.owner_key = "replacement-owner"
        await db.commit()

    with pytest.raises(BudgetBindingConflict):
        await settle_llm_call_with_usage_receipt(
            factory,
            work_order_id=order_id,
            operation_key=reservation.operation_key,
            expected_owner_key="usage-owner",
            evidence=_known(),
        )
    async with factory() as db:
        persisted = await db.get(WorkBudgetReservation, reservation.id)
        assert persisted.state == "reserved"
        assert persisted.actual_units is None
        assert await db.get(WorkEvent, usage_event_id(reservation.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["charged", "unknown"])
async def test_legacy_or_unknown_settlement_cannot_be_reopened(test_engine, prior):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    reservation = await _reserve(factory, order_id, f"llm:{prior}")
    await settle_budget(
        factory,
        work_order_id=order_id,
        operation_key=reservation.operation_key,
        **({"actual_units": 1} if prior == "charged" else {"unknown": True}),
    )

    with pytest.raises(BudgetReservationConflict):
        await settle_llm_call_with_usage_receipt(
            factory,
            work_order_id=order_id,
            operation_key=reservation.operation_key,
            expected_owner_key="usage-owner",
            evidence=_known(),
        )
    async with factory() as db:
        assert await db.get(WorkEvent, usage_event_id(reservation.id)) is None


@pytest.mark.asyncio
async def test_lowered_limit_after_reserve_persists_charge_blocker(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory, budgets={"max_llm_calls": 1})
    reservation = await _reserve(factory, order_id, "llm:limit-lowered")
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id)
        ledger.max_llm_calls = 0
        await db.commit()

    charged, _ = await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=order_id,
        operation_key=reservation.operation_key,
        expected_owner_key="usage-owner",
        evidence=_known(),
    )
    assert charged.blocker["code"] == "budget_charge_exceeded"
    async with factory() as db:
        order = await db.get(WorkOrder, order_id)
        ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id)
        assert ledger.blocker["code"] == "budget_charge_exceeded"


@pytest.mark.asyncio
async def test_event_construction_failure_rolls_back_settlement(test_engine, monkeypatch):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    reservation = await _reserve(factory, order_id, "llm:rollback")
    real_flush = factory.class_.flush

    async def broken_flush(session, *args, **kwargs):
        if any(isinstance(row, WorkEvent) for row in session.new):
            raise RuntimeError("event flush failed")
        return await real_flush(session, *args, **kwargs)

    monkeypatch.setattr(factory.class_, "flush", broken_flush)
    with pytest.raises(RuntimeError, match="event flush failed"):
        await settle_llm_call_with_usage_receipt(
            factory,
            work_order_id=order_id,
            operation_key=reservation.operation_key,
            expected_owner_key="usage-owner",
            evidence=_known(),
        )
    async with factory() as db:
        persisted = await db.get(WorkBudgetReservation, reservation.id)
        assert persisted.state == "reserved"
        assert persisted.actual_units is None


@pytest.mark.asyncio
async def test_shared_ledger_aggregate_is_owner_bound_and_conservative(test_engine):
    factory = _factory(test_engine)
    root_id, child_id, ledger_id = await _lineage(factory)
    known = await _reserve(factory, root_id, "llm:root-known")
    partial = await _reserve(factory, child_id, "llm:child-partial")
    await _reserve(factory, child_id, "llm:child-crash")
    await _reserve(factory, child_id, "llm:unsupported-batch", units=2)
    await _reserve(factory, child_id, "execution:zero-fence", units=0)
    await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=root_id,
        operation_key=known.operation_key,
        expected_owner_key="usage-owner",
        evidence=_known(2, 3),
    )
    await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=child_id,
        operation_key=partial.operation_key,
        expected_owner_key="usage-owner",
        evidence=ollama_usage_from_body({"done": False, "prompt_eval_count": 4}),
    )

    summary = await read_recorded_llm_usage(
        factory, work_order_id=child_id, expected_owner_key="usage-owner"
    )
    assert summary["ledger_id"] == str(ledger_id)
    assert summary["coverage"] == {
        "scope": "recorded_physical_llm_reservations",
        "complete_for_scope": False,
        "system_wide": "not_claimed",
    }
    assert summary["attempts"] == {
        "recorded": 4,
        "known_total": 1,
        "unknown": 3,
        "missing_receipts": 1,
        "invalid_receipts": 0,
        "reserved": 1,
        "unsupported_reservations": 1,
    }
    assert summary["tokens"] == {
        "status": "partial",
        "input_known_lower_bound": 6,
        "output_known_lower_bound": 3,
        "known_lower_bound": 9,
        "total": None,
    }
    assert summary["cost"]["status"] == "unknown"
    assert summary["cost"]["total"] is None
    with pytest.raises(BudgetBindingConflict):
        await read_recorded_llm_usage(
            factory, work_order_id=root_id, expected_owner_key="different-owner"
        )


@pytest.mark.asyncio
async def test_invalid_receipt_payload_never_counts_as_known(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    reservation = await _reserve(factory, order_id, "llm:tampered")
    _, event = await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=order_id,
        operation_key=reservation.operation_key,
        expected_owner_key="usage-owner",
        evidence=unknown_ollama_usage("response_not_observed"),
    )
    async with factory() as db:
        persisted = await db.get(WorkEvent, event.id)
        persisted.payload = {
            **persisted.payload,
            "tokens": {
                "input": {"status": "known", "units": 0},
                "output": {"status": "known", "units": 0},
                "total": {"status": "known", "units": 0},
            },
            "forged": True,
        }
        await db.commit()

    summary = await read_recorded_llm_usage(
        factory, work_order_id=order_id, expected_owner_key="usage-owner"
    )
    assert summary["attempts"]["invalid_receipts"] == 1
    assert summary["tokens"]["status"] == "unknown"
    assert summary["tokens"]["total"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("total_units", [True, 1.0])
async def test_non_integer_total_is_invalid_and_contributes_no_lower_bound(
    test_engine, total_units
):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    reservation = await _reserve(factory, order_id, f"llm:bad-total-{type(total_units).__name__}")
    _, event = await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=order_id,
        operation_key=reservation.operation_key,
        expected_owner_key="usage-owner",
        evidence=_known(0, 1),
    )
    async with factory() as db:
        persisted = await db.get(WorkEvent, event.id)
        tokens = {key: dict(value) for key, value in persisted.payload["tokens"].items()}
        tokens["total"] = {"status": "known", "units": total_units}
        persisted.payload = {**persisted.payload, "tokens": tokens}
        flag_modified(persisted, "payload")
        await db.commit()

    with pytest.raises(BudgetReservationConflict):
        await settle_llm_call_with_usage_receipt(
            factory,
            work_order_id=order_id,
            operation_key=reservation.operation_key,
            expected_owner_key="usage-owner",
            evidence=_known(0, 1),
        )
    summary = await read_recorded_llm_usage(
        factory, work_order_id=order_id, expected_owner_key="usage-owner"
    )
    assert summary["attempts"]["invalid_receipts"] == 1
    assert summary["attempts"]["known_total"] == 0
    assert summary["tokens"]["known_lower_bound"] == 0
    assert summary["tokens"]["total"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("component", "reason"),
    [
        ("input", ["missing"]),
        ("output", {"reason": "missing"}),
        ("total", ["terminal_not_observed"]),
        ("total", {"reason": "terminal_not_observed"}),
    ],
)
async def test_malformed_unknown_reason_is_invalid_instead_of_raising(
    test_engine, component, reason
):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    reservation = await _reserve(
        factory,
        order_id,
        f"llm:bad-reason-{component}-{type(reason).__name__}",
    )
    _, event = await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=order_id,
        operation_key=reservation.operation_key,
        expected_owner_key="usage-owner",
        evidence=unknown_ollama_usage("response_not_observed"),
    )
    async with factory() as db:
        persisted = await db.get(WorkEvent, event.id)
        tokens = {key: dict(value) for key, value in persisted.payload["tokens"].items()}
        tokens[component] = {"status": "unknown", "reason": reason}
        persisted.payload = {**persisted.payload, "tokens": tokens}
        await db.commit()

    summary = await read_recorded_llm_usage(
        factory, work_order_id=order_id, expected_owner_key="usage-owner"
    )
    assert summary["attempts"]["invalid_receipts"] == 1
    assert summary["tokens"]["known_lower_bound"] == 0
    assert summary["tokens"]["total"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "reserved"),
        ("actual_units", Decimal(2)),
        ("settlement_digest", "0" * 64),
    ],
)
async def test_inconsistent_settlement_does_not_contribute_known_lower_bound(
    test_engine, field, value
):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    reservation = await _reserve(factory, order_id, f"llm:tampered-{field}")
    await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=order_id,
        operation_key=reservation.operation_key,
        expected_owner_key="usage-owner",
        evidence=_known(11, 13),
    )
    async with factory() as db:
        persisted = await db.get(WorkBudgetReservation, reservation.id)
        setattr(persisted, field, value)
        await db.commit()

    summary = await read_recorded_llm_usage(
        factory, work_order_id=order_id, expected_owner_key="usage-owner"
    )
    assert summary["attempts"]["invalid_receipts"] == 1
    assert summary["tokens"]["known_lower_bound"] == 0
    assert summary["tokens"]["total"] is None
