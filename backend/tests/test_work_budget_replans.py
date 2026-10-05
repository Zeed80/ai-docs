"""E21: replans are charged to the shared lineage ledger, not per order only."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import WorkOrder
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.work_budget_ledger import initialize_budget_ledger
from app.domain.work_orders import WorkStateError, _replan_or_block, create_work_order


async def _lineage(factory, *, root_budgets=None, child_budgets=None, constraints=None):
    """A root and one child bound to the same ledger at intake."""
    async with factory() as db:
        root = await create_work_order(
            db,
            owner_key="local:alice",
            objective="root",
            budgets=root_budgets or {},
            constraints=constraints or {},
        )
        child = await create_work_order(
            db,
            owner_key="local:alice",
            objective="child",
            budgets=child_budgets or {},
            parent_id=root.id,
        )
        ledger = await initialize_budget_ledger(db, child.id)
        await db.commit()
        return root.id, child.id, ledger.id


async def _fail(factory, order_id, *, status="running", revision=1, guarded=True):
    async with factory() as db:
        order = await db.get(WorkOrder, order_id, with_for_update=True)
        order.status = status
        order.plan_revision = revision
        order.blocker = {"code": "step_failed"}
        await _replan_or_block(db, order, actor="test", guarded=guarded)
        await db.commit()
        return order.status, order.blocker


async def _replans(factory, ledger_id):
    async with factory() as db:
        return list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.ledger_id == ledger_id,
                    WorkBudgetReservation.dimension == "replans",
                )
            )
        )


@pytest.mark.asyncio
async def test_child_cannot_spend_beyond_the_shared_replan_allowance(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    root_id, child_id, ledger_id = await _lineage(
        factory, root_budgets={"max_replans": 1}, child_budgets={"max_replans": 5}
    )

    assert (await _fail(factory, root_id))[0] == "replanning"
    status, blocker = await _fail(factory, child_id)

    # The child's own count would allow it; the shared ledger does not.
    assert status == "blocked"
    assert blocker == {"code": "replan_budget_exhausted", "cause": {"code": "step_failed"}}
    rows = await _replans(factory, ledger_id)
    assert [(row.work_order_id, row.state, row.actual_units) for row in rows] == [
        (root_id, "charged", 1)
    ]
    async with factory() as db:
        ledger = await db.get(WorkBudgetLedger, ledger_id)
        assert ledger.blocker is None  # exhaustion blocks the order, not the lineage


@pytest.mark.asyncio
async def test_same_revision_is_charged_once(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    root_id, _child_id, ledger_id = await _lineage(factory, root_budgets={"max_replans": 3})

    await _fail(factory, root_id, revision=1)
    await _fail(factory, root_id, revision=1)
    await _fail(factory, root_id, revision=2)

    keys = sorted(row.operation_key for row in await _replans(factory, ledger_id))
    assert keys == [f"replan:{root_id}:from-r1", f"replan:{root_id}:from-r2"]


@pytest.mark.asyncio
async def test_charge_rolls_back_with_an_illegal_transition(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    root_id, _child_id, ledger_id = await _lineage(factory, root_budgets={"max_replans": 3})

    with pytest.raises(WorkStateError):
        await _fail(factory, root_id, status="completed", guarded=False)
    assert await _replans(factory, ledger_id) == []

    # Guarded callers skip the illegal transition and charge nothing.
    assert (await _fail(factory, root_id, status="completed"))[0] == "completed"
    assert await _replans(factory, ledger_id) == []


@pytest.mark.asyncio
async def test_per_order_limit_still_applies_and_charges_nothing(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    root_id, _child_id, ledger_id = await _lineage(factory, root_budgets={"max_replans": 0})

    assert (await _fail(factory, root_id))[0] == "blocked"
    assert await _replans(factory, ledger_id) == []


@pytest.mark.asyncio
async def test_exploratory_root_keeps_its_generous_replan_default(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    _root, _child, ledger_id = await _lineage(factory, constraints={"mode": "exploratory"})
    _root2, _child2, plain_ledger_id = await _lineage(factory)

    async with factory() as db:
        assert (await db.get(WorkBudgetLedger, ledger_id)).max_replans == 30
        assert (await db.get(WorkBudgetLedger, plain_ledger_id)).max_replans == 3
