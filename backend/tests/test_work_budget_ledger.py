"""E21.1: atomic, shared budget-ledger primitives (not runtime wiring)."""

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import WorkOrder
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.work_budget_ledger import (
    BudgetBindingConflict,
    BudgetExceeded,
    BudgetLedgerError,
    BudgetReservationConflict,
    LegacyBudgetBaselineRequired,
    bind_budget_ledger,
    normalize_limits,
    reserve_budget,
    settle_budget,
)
from app.domain.work_orders import claim_ready_step, create_single_step_plan, create_work_order

REQUEST_DIGEST = "a" * 64


def _factory(test_engine):
    return async_sessionmaker(test_engine, expire_on_commit=False)


async def _fresh_order(factory, *, budgets=None, parent_id=None, metadata=None):
    async with factory() as db:
        order = await create_work_order(
            db,
            owner_key="budget-test-owner",
            objective=f"budget test {uuid.uuid4()}",
            budgets=budgets,
            parent_id=parent_id,
            metadata=metadata,
        )
        await db.commit()
        return order.id


async def _cleanup(factory, order_ids):
    async with factory() as db:
        ledger_ids = list(
            await db.scalars(
                select(WorkOrder.budget_ledger_id).where(
                    WorkOrder.id.in_(order_ids), WorkOrder.budget_ledger_id.is_not(None)
                )
            )
        )
        if ledger_ids:
            await db.execute(
                delete(WorkBudgetReservation).where(WorkBudgetReservation.ledger_id.in_(ledger_ids))
            )
            await db.execute(
                update(WorkOrder)
                .where(WorkOrder.budget_ledger_id.in_(ledger_ids))
                .values(budget_ledger_id=None)
            )
            await db.execute(delete(WorkBudgetLedger).where(WorkBudgetLedger.id.in_(ledger_ids)))
        # Children first because parent_id is intentionally authoritative.
        for order_id in reversed(order_ids):
            row = await db.get(WorkOrder, order_id)
            if row is not None:
                await db.delete(row)
        await db.commit()


def test_limit_defaults_and_explicit_zero_are_exact_decimals():
    defaults = normalize_limits({})
    assert defaults == {
        "max_active_seconds": Decimal("7200"),
        "max_tool_attempts": Decimal("200"),
        "max_llm_calls": Decimal("50"),
        "max_replans": Decimal("3"),
        "max_tokens": None,
        "max_cost_usd": None,
    }
    explicit = normalize_limits(
        {
            "max_replans": 0,
            "max_llm_calls": 0,
            "token_budget": 0,
            "max_cost_usd": "0.00000000",
        }
    )
    assert explicit["max_replans"] == 0
    assert explicit["max_llm_calls"] == 0
    assert explicit["max_tokens"] == 0
    assert explicit["max_cost_usd"] == Decimal("0.00000000")


@pytest.mark.parametrize(
    "budgets",
    [
        {"max_llm_calls": True},
        {"max_llm_calls": 1.5},
        {"max_cost_usd": -1},
        {"max_cost_usd": "NaN"},
        {"max_cost_usd": "Infinity"},
        {"max_cost_usd": "0.000000001"},
        {"max_cost_usd": "10000000000000000000000"},
    ],
)
def test_limit_validation_rejects_implicit_rounding_and_non_finite_values(budgets):
    with pytest.raises(BudgetLedgerError):
        normalize_limits(budgets)


@pytest.mark.asyncio
async def test_parent_and_child_bind_to_one_server_resolved_ledger(test_engine):
    factory = _factory(test_engine)
    root_id = await _fresh_order(factory, metadata={"budget_root_id": str(uuid.uuid4())})
    child_id = await _fresh_order(
        factory,
        parent_id=root_id,
        metadata={"budget_root_id": str(uuid.uuid4())},
    )
    try:
        root_ledger, child_ledger = await asyncio.gather(
            bind_budget_ledger(factory, root_id), bind_budget_ledger(factory, child_id)
        )
        assert root_ledger.id == child_ledger.id
        assert root_ledger.root_work_order_id == root_id
        async with factory() as db:
            rows = list(
                await db.scalars(select(WorkOrder).where(WorkOrder.id.in_([root_id, child_id])))
            )
            assert {row.budget_ledger_id for row in rows} == {root_ledger.id}
    finally:
        await _cleanup(factory, [root_id, child_id])


@pytest.mark.asyncio
async def test_tighter_descendant_limit_requires_reconciliation(test_engine):
    factory = _factory(test_engine)
    root_id = await _fresh_order(factory, budgets={"max_llm_calls": 50})
    child_id = await _fresh_order(factory, parent_id=root_id, budgets={"max_llm_calls": 2})
    try:
        with pytest.raises(BudgetBindingConflict, match="tighter max_llm_calls"):
            await bind_budget_ledger(factory, child_id)
    finally:
        await _cleanup(factory, [root_id, child_id])


@pytest.mark.asyncio
async def test_any_historic_sibling_usage_rejects_empty_ledger_binding(test_engine):
    factory = _factory(test_engine)
    root_id = await _fresh_order(factory)
    requested_id = await _fresh_order(factory, parent_id=root_id)
    spent_sibling_id = await _fresh_order(factory, parent_id=root_id)
    try:
        async with factory() as db:
            sibling = await db.get(WorkOrder, spent_sibling_id)
            await create_single_step_plan(
                db, sibling, kind="agent_turn", title="started", input_data={"prompt": "x"}
            )
            await db.commit()
        async with factory() as db:
            claimed = await claim_ready_step(
                db, worker_id="budget-test-worker", work_order_id=spent_sibling_id
            )
            assert claimed is not None
            await db.commit()
        with pytest.raises(LegacyBudgetBaselineRequired):
            await bind_budget_ledger(factory, requested_id)
    finally:
        await _cleanup(factory, [root_id, requested_id, spent_sibling_id])


@pytest.mark.asyncio
async def test_execution_status_without_attempt_still_requires_legacy_baseline(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    try:
        async with factory() as db:
            order = await db.get(WorkOrder, order_id)
            order.status = "running"
            order.started_at = datetime.now(UTC)
            await db.commit()
        with pytest.raises(LegacyBudgetBaselineRequired):
            await bind_budget_ledger(factory, order_id)
    finally:
        await _cleanup(factory, [order_id])


@pytest.mark.asyncio
async def test_existing_plan_revision_without_attempt_requires_legacy_baseline(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    try:
        async with factory() as db:
            order = await db.get(WorkOrder, order_id)
            await create_single_step_plan(
                db, order, kind="agent_turn", title="planned", input_data={"prompt": "x"}
            )
            await db.commit()
        with pytest.raises(LegacyBudgetBaselineRequired):
            await bind_budget_ledger(factory, order_id)
    finally:
        await _cleanup(factory, [order_id])


@pytest.mark.asyncio
async def test_cross_owner_child_lineage_is_rejected(test_engine):
    factory = _factory(test_engine)
    root_id = await _fresh_order(factory)
    child_id = await _fresh_order(factory, parent_id=root_id)
    try:
        async with factory() as db:
            child = await db.get(WorkOrder, child_id)
            child.owner_key = "different-owner"
            await db.commit()
        with pytest.raises(BudgetBindingConflict, match="owner differs"):
            await bind_budget_ledger(factory, child_id)
    finally:
        await _cleanup(factory, [root_id, child_id])


@pytest.mark.asyncio
async def test_operation_binding_and_settlement_are_idempotent_but_conflicts_fail(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    try:
        await bind_budget_ledger(factory, order_id)
        first = await reserve_budget(
            factory,
            work_order_id=order_id,
            operation_key="llm:one",
            dimension="llm_calls",
            units=1,
            request_digest=REQUEST_DIGEST,
        )
        repeated = await reserve_budget(
            factory,
            work_order_id=order_id,
            operation_key="llm:one",
            dimension="llm_calls",
            units=Decimal("1.0"),
            request_digest=REQUEST_DIGEST,
        )
        assert repeated.id == first.id
        with pytest.raises(BudgetReservationConflict):
            await reserve_budget(
                factory,
                work_order_id=order_id,
                operation_key="llm:one",
                dimension="tokens",
                units=1,
                request_digest=REQUEST_DIGEST,
            )
        with pytest.raises(BudgetReservationConflict):
            await reserve_budget(
                factory,
                work_order_id=order_id,
                operation_key="llm:one",
                dimension="llm_calls",
                units=1,
                request_digest="b" * 64,
            )
        charged = await settle_budget(
            factory, work_order_id=order_id, operation_key="llm:one", actual_units=1
        )
        charged_again = await settle_budget(
            factory, work_order_id=order_id, operation_key="llm:one", actual_units=1
        )
        assert charged_again.id == charged.id
        with pytest.raises(BudgetReservationConflict):
            await settle_budget(
                factory, work_order_id=order_id, operation_key="llm:one", actual_units=0
            )
        exact_a = Decimal("1234567890123456789012.12345678")
        exact_b = Decimal("1234567890123456789012.12345679")
        await reserve_budget(
            factory,
            work_order_id=order_id,
            operation_key="cost:30-digits",
            dimension="cost_usd",
            units=exact_a,
            request_digest=REQUEST_DIGEST,
        )
        with pytest.raises(BudgetReservationConflict):
            await reserve_budget(
                factory,
                work_order_id=order_id,
                operation_key="cost:30-digits",
                dimension="cost_usd",
                units=exact_b,
                request_digest=REQUEST_DIGEST,
            )
        await settle_budget(
            factory,
            work_order_id=order_id,
            operation_key="cost:30-digits",
            actual_units=exact_a,
        )
        with pytest.raises(BudgetReservationConflict):
            await settle_budget(
                factory,
                work_order_id=order_id,
                operation_key="cost:30-digits",
                actual_units=exact_b,
            )
    finally:
        await _cleanup(factory, [order_id])


@pytest.mark.asyncio
async def test_concurrent_last_slot_is_reserved_once_across_parent_and_child(test_engine):
    factory = _factory(test_engine)
    root_id = await _fresh_order(factory, budgets={"max_tool_attempts": 1})
    child_id = await _fresh_order(factory, parent_id=root_id)
    try:
        await bind_budget_ledger(factory, root_id)

        async def attempt(order_id, key):
            try:
                row = await reserve_budget(
                    factory,
                    work_order_id=order_id,
                    operation_key=key,
                    dimension="tool_attempts",
                    units=1,
                    request_digest=REQUEST_DIGEST,
                )
                return row.id
            except BudgetExceeded:
                return None

        results = await asyncio.gather(
            attempt(root_id, "tool:root"), attempt(child_id, "tool:child")
        )
        assert sum(item is not None for item in results) == 1
        async with factory() as db:
            rows = list(await db.scalars(select(WorkBudgetReservation)))
            assert len([row for row in rows if row.operation_key.startswith("tool:")]) == 1
    finally:
        await _cleanup(factory, [root_id, child_id])


@pytest.mark.asyncio
async def test_unknown_consumes_full_reserve_and_cost_unknown_is_not_zero(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory, budgets={"max_cost_usd": "1.0"})
    try:
        ledger = await bind_budget_ledger(factory, order_id)
        await reserve_budget(
            factory,
            work_order_id=order_id,
            operation_key="cost:crash",
            dimension="cost_usd",
            units="1.0",
            request_digest=REQUEST_DIGEST,
        )
        unknown = await settle_budget(
            factory, work_order_id=order_id, operation_key="cost:crash", unknown=True
        )
        assert unknown.actual_units is None
        assert unknown.actual_unknown is True
        with pytest.raises(BudgetExceeded):
            await reserve_budget(
                factory,
                work_order_id=order_id,
                operation_key="cost:next",
                dimension="cost_usd",
                units="0.01",
                request_digest=REQUEST_DIGEST,
            )
        async with factory() as db:
            persisted = await db.get(WorkBudgetLedger, ledger.id)
            assert persisted.blocker["code"] in {
                "cost_usage_unknown",
                "budget_reservation_exceeded",
            }
    finally:
        await _cleanup(factory, [order_id])


@pytest.mark.asyncio
async def test_known_actual_overrun_is_persisted_and_blocks_ledger(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory, budgets={"max_cost_usd": "10"})
    try:
        ledger = await bind_budget_ledger(factory, order_id)
        await reserve_budget(
            factory,
            work_order_id=order_id,
            operation_key="cost:overrun",
            dimension="cost_usd",
            units="1",
            request_digest=REQUEST_DIGEST,
        )
        charged = await settle_budget(
            factory,
            work_order_id=order_id,
            operation_key="cost:overrun",
            actual_units="2.5",
        )
        assert charged.actual_units == Decimal("2.5")
        assert charged.blocker["code"] == "budget_actual_overrun"
        async with factory() as db:
            persisted = await db.get(WorkBudgetLedger, ledger.id)
            assert persisted.blocker["actual"] == "2.5"
    finally:
        await _cleanup(factory, [order_id])


@pytest.mark.asyncio
async def test_numeric30_boundary_sum_and_actual_blocker_are_exact(test_engine):
    factory = _factory(test_engine)
    limit = Decimal("1234567890123456789012.12345678")
    over = Decimal("1234567890123456789012.12345679")
    order_id = await _fresh_order(factory, budgets={"max_cost_usd": str(limit)})
    try:
        ledger = await bind_budget_ledger(factory, order_id)
        await reserve_budget(
            factory,
            work_order_id=order_id,
            operation_key="cost:boundary",
            dimension="cost_usd",
            units=limit,
            request_digest=REQUEST_DIGEST,
        )
        with pytest.raises(BudgetExceeded):
            await reserve_budget(
                factory,
                work_order_id=order_id,
                operation_key="cost:last-fraction",
                dimension="cost_usd",
                units=Decimal("0.00000001"),
                request_digest=REQUEST_DIGEST,
            )
        charged = await settle_budget(
            factory,
            work_order_id=order_id,
            operation_key="cost:boundary",
            actual_units=over,
        )
        assert charged.actual_units == over
        assert charged.blocker["code"] == "budget_charge_exceeded"
        assert Decimal(charged.blocker["charged_or_reserved"]) == over
        async with factory() as db:
            persisted = await db.get(WorkBudgetLedger, ledger.id)
            assert Decimal(persisted.blocker["charged_or_reserved"]) == over
    finally:
        await _cleanup(factory, [order_id])


@pytest.mark.asyncio
async def test_reservation_validation_rejects_fractional_counts_bool_and_non_finite(test_engine):
    factory = _factory(test_engine)
    order_id = await _fresh_order(factory)
    try:
        await bind_budget_ledger(factory, order_id)
        for index, units in enumerate(
            (True, Decimal("1.5"), Decimal("NaN"), Decimal("Infinity"), -1)
        ):
            with pytest.raises(BudgetLedgerError):
                await reserve_budget(
                    factory,
                    work_order_id=order_id,
                    operation_key=f"invalid:{index}",
                    dimension="llm_calls",
                    units=units,
                    request_digest=REQUEST_DIGEST,
                )
    finally:
        await _cleanup(factory, [order_id])


@pytest.mark.asyncio
async def test_budget_migration_upgrade_downgrade_upgrade_in_isolated_schema(test_engine):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[1] / "migrations/versions/20261001_0001_work_budget_ledger.py"
    spec = importlib.util.spec_from_file_location("work_budget_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "work_budget_migration_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await connection.execute(text("CREATE TABLE work_orders (id UUID PRIMARY KEY)"))

        def migrate(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.upgrade()
                inspector = inspect(sync)
                assert "work_budget_ledgers" in inspector.get_table_names()
                assert "work_budget_reservations" in inspector.get_table_names()
                assert "budget_ledger_id" in {
                    column["name"] for column in inspector.get_columns("work_orders")
                }
                module.downgrade()
                inspector = inspect(sync)
                assert "work_budget_ledgers" not in inspector.get_table_names()
                assert "budget_ledger_id" not in {
                    column["name"] for column in inspector.get_columns("work_orders")
                }
                module.upgrade()

        await connection.run_sync(migrate)
        await connection.rollback()
