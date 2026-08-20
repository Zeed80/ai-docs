"""Б11: team decomposition — a "decompose" step spawns child WorkOrders, the
parent waits (status "waiting_external") until every child reaches a
terminal state, then completes (or fails) with an aggregated summary.

Uses test_engine + separate committed sessions throughout, not the
db_session fixture (a single transaction rolled back at teardown): every
DB-touching test here calls _execute_decompose at least once, and that
function always opens its own fresh session via _get_session_factory() (by
design — it runs as a Celery task body, not inside a caller's shared
transaction) and commits internally. A session on db_session's uncommitted
transaction and _execute_decompose's separately-committed one can't see each
other's writes, so mixing them silently breaks — same reasoning as the
concurrent-claim test in test_work_order_lease.py.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import WorkOrder
from app.domain.work_orders import (
    _blocker_reason,
    claim_ready_step,
    complete_attempt,
    create_single_step_plan,
    create_work_order,
    enter_waiting_for_children,
    exploratory_acceptance_criteria,
    fail_attempt,
    promote_waiting_parents,
    transition_work_order,
    verify_nonempty_result,
)
from app.domain.work_planning import PlannedStep
from app.tasks.work_orders import _execute_decompose, _split_child_budgets


def test_blocker_reason_none_for_no_blocker():
    assert _blocker_reason(None) is None


def test_blocker_reason_digs_into_step_failed_wrapper():
    assert _blocker_reason({"code": "step_failed", "error": {"code": "no_grant"}}) == "no_grant"


def test_blocker_reason_falls_back_to_error_message_without_a_code():
    assert (
        _blocker_reason({"code": "step_failed", "error": {"message": "timed out"}})
        == "timed out"
    )


def test_blocker_reason_uses_top_level_code_for_non_step_failed_blockers():
    assert _blocker_reason({"code": "token_budget_exceeded", "tokens_spent": 500}) == "token_budget_exceeded"


def test_planned_step_decompose_requires_nonempty_children():
    with pytest.raises(ValidationError, match="children"):
        PlannedStep(
            step_key="fanout", title="Fan out", kind="decompose", input={},
        )


def test_planned_step_decompose_accepts_children_list():
    step = PlannedStep(
        step_key="fanout",
        title="Fan out",
        kind="decompose",
        input={"children": [{"objective": "Собрать данные по поставщику А"}]},
    )
    assert step.kind == "decompose"


def test_split_child_budgets_divides_evenly():
    assert _split_child_budgets({"token_budget": 900}, 3, None) == {"token_budget": 300}


def test_split_child_budgets_rounds_down_but_never_to_zero():
    assert _split_child_budgets({"token_budget": 2}, 5, None) == {"token_budget": 1}


def test_split_child_budgets_explicit_override_wins():
    assert _split_child_budgets(
        {"token_budget": 900}, 3, {"token_budget": 50}
    ) == {"token_budget": 50}


def test_split_child_budgets_no_parent_budget_means_no_child_budget():
    assert _split_child_budgets({}, 3, None) == {}


@pytest.mark.asyncio
async def test_decompose_creates_children_with_split_budget_and_parent_waits(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    # _execute_decompose opens its own session via app.db.session._get_session_factory
    # (production code, cached engine pointed at settings.database_url) — force it onto
    # this test's NullPool test_engine instead, or it can hand out a connection from a
    # different, possibly-dirty pooled engine ("another operation is in progress").
    import app.db.session as _db_session_module
    monkeypatch.setattr(_db_session_module, "_get_session_factory", lambda: factory)
    children_input = {
        "children": [
            {"objective": "Поставщик А: собрать счета"},
            {"objective": "Поставщик Б: собрать счета"},
        ]
    }
    async with factory() as db:
        order = await create_work_order(
            db, owner_key="tester", objective="Собрать отчёт по двум поставщикам",
            priority=80, risk_level="medium", budgets={"token_budget": 1000},
        )
        await create_single_step_plan(
            db, order, kind="decompose", title="Fan out", input_data=children_input,
        )
        order_id = order.id
        await db.commit()

    output = await _execute_decompose(order_id, children_input)
    assert len(output["child_order_ids"]) == 2

    async with factory() as db:
        children = list(
            (
                await db.execute(select(WorkOrder).where(WorkOrder.parent_id == order_id))
            ).scalars()
        )
        assert len(children) == 2
        assert {c.objective for c in children} == {
            "Поставщик А: собрать счета", "Поставщик Б: собрать счета",
        }
        for child in children:
            # A child never outranks the parent that spawned it.
            assert child.priority == 80
            assert child.risk_level == "medium"
            assert child.budgets == {"token_budget": 500}  # 1000 split across 2
            assert child.status == "ready"  # create_single_step_plan already advanced it


@pytest.mark.asyncio
async def test_decompose_children_inherit_exploratory_constraints(test_engine, monkeypatch):
    """Ф4 finding: a child spawned from an exploratory objective is itself
    still exploratory (small but open-ended) — matters if its own initial
    agent_turn step later fails and gets replanned."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    import app.db.session as _db_session_module
    monkeypatch.setattr(_db_session_module, "_get_session_factory", lambda: factory)
    children_input = {"children": [{"objective": "Поставщик А: найди каталог"}]}
    async with factory() as db:
        order = await create_work_order(
            db, owner_key="tester", objective="Найди каталоги поставщиков",
            constraints={"mode": "exploratory"},
        )
        await create_single_step_plan(
            db, order, kind="decompose", title="Fan out", input_data=children_input,
        )
        order_id = order.id
        await db.commit()

    await _execute_decompose(order_id, children_input)

    async with factory() as db:
        child = (
            await db.execute(select(WorkOrder).where(WorkOrder.parent_id == order_id))
        ).scalar_one()
        assert child.constraints == {"mode": "exploratory"}


@pytest.mark.asyncio
async def test_promote_waiting_parents_completes_once_all_children_succeed(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    # _execute_decompose opens its own session via app.db.session._get_session_factory
    # (production code, cached engine pointed at settings.database_url) — force it onto
    # this test's NullPool test_engine instead, or it can hand out a connection from a
    # different, possibly-dirty pooled engine ("another operation is in progress").
    import app.db.session as _db_session_module
    monkeypatch.setattr(_db_session_module, "_get_session_factory", lambda: factory)
    children_input = {"children": [{"objective": "Child one"}, {"objective": "Child two"}]}
    async with factory() as db:
        parent = await create_work_order(db, owner_key="tester", objective="Parent")
        await create_single_step_plan(
            db, parent, kind="decompose", title="Fan out", input_data=children_input,
        )
        parent_id = parent.id
        await db.commit()

    output = await _execute_decompose(parent_id, children_input)

    async with factory() as db:
        parent = await db.get(WorkOrder, parent_id)
        await transition_work_order(db, parent, "running", actor="test")
        await enter_waiting_for_children(db, order=parent, actor="test")
        await db.commit()
        assert parent.status == "waiting_external"

    async with factory() as db:
        children = list(
            (
                await db.execute(
                    select(WorkOrder).where(WorkOrder.id.in_(output["child_order_ids"]))
                )
            ).scalars()
        )
        assert len(children) == 2

        # Not done yet — no child has even started.
        still_waiting = await promote_waiting_parents(db)
        await db.commit()
        assert still_waiting == 0

        for child in children:
            claimed = await claim_ready_step(db, worker_id="w", work_order_id=child.id)
            assert claimed is not None
            c_order, c_step, c_attempt = claimed
            await complete_attempt(
                db, order=c_order, step=c_step, attempt=c_attempt,
                output={"text": "готово"}, actor="w",
            )
            await verify_nonempty_result(db, order=c_order, step=c_step)
        await db.commit()
        assert all(c.status == "completed" for c in children)

        promoted = await promote_waiting_parents(db)
        await db.commit()

    async with factory() as db:
        parent = await db.get(WorkOrder, parent_id)
        assert promoted == 1
        assert parent.status == "completed"
        assert "Child one" in parent.result_summary
        assert "Child two" in parent.result_summary


@pytest.mark.asyncio
async def test_promote_waiting_parents_builds_structured_coverage_for_exploratory_parent(
    test_engine, monkeypatch
):
    """Ф4 finding: exploratory_acceptance_criteria()'s coverage_report
    predicate needs output.coverage = {covered, partial, not_found} — before
    this fix, promote_waiting_parents only ever produced a human-readable
    text summary, so a decompose-fan-out exploratory objective (the pattern
    the planner prompt itself steers toward — see work_planning.py) could
    never satisfy that criterion. One child succeeds, one is blocked with a
    real reason; both must show up in the right coverage bucket."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    import app.db.session as _db_session_module
    monkeypatch.setattr(_db_session_module, "_get_session_factory", lambda: factory)
    children_input = {
        "children": [
            {"objective": "Поставщик А: найди каталог"},
            {"objective": "Поставщик Б: найди каталог", "budgets": {"max_replans": 0}},
        ]
    }
    async with factory() as db:
        parent = await create_work_order(
            db,
            owner_key="tester",
            objective="Найди каталоги поставщиков",
            constraints={"mode": "exploratory"},
            acceptance_criteria=exploratory_acceptance_criteria(),
        )
        await create_single_step_plan(
            db, parent, kind="decompose", title="Fan out", input_data=children_input,
        )
        parent_id = parent.id
        await db.commit()

    output = await _execute_decompose(parent_id, children_input)

    async with factory() as db:
        parent = await db.get(WorkOrder, parent_id)
        await transition_work_order(db, parent, "running", actor="test")
        await enter_waiting_for_children(db, order=parent, actor="test")
        children = list(
            (
                await db.execute(
                    select(WorkOrder).where(WorkOrder.id.in_(output["child_order_ids"]))
                )
            ).scalars()
        )
        await db.commit()

    ok_child = next(c for c in children if "А" in c.objective)
    bad_child = next(c for c in children if "Б" in c.objective)

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w", work_order_id=ok_child.id)
        assert claimed is not None
        c_order, c_step, c_attempt = claimed
        await complete_attempt(
            db, order=c_order, step=c_step, attempt=c_attempt, output={"text": "готово"}, actor="w",
        )
        await verify_nonempty_result(db, order=c_order, step=c_step)

        claimed2 = await claim_ready_step(db, worker_id="w", work_order_id=bad_child.id)
        assert claimed2 is not None
        b_order, b_step, b_attempt = claimed2
        await fail_attempt(
            db, order=b_order, step=b_step, attempt=b_attempt,
            error={"code": "no_grant"}, retryable=False, actor="w",
        )
        await db.commit()

        promoted = await promote_waiting_parents(db)
        await db.commit()
        assert promoted == 1

    async with factory() as db:
        from app.db.models import WorkStep

        decompose_step = (
            await db.execute(
                select(WorkStep).where(WorkStep.work_order_id == parent_id, WorkStep.kind == "decompose")
            )
        ).scalar_one()
        coverage = decompose_step.output["coverage"]
        assert coverage["covered"] == ["Поставщик А: найди каталог"]
        assert coverage["partial"] == []
        assert len(coverage["not_found"]) == 1
        assert coverage["not_found"][0]["item"] == "Поставщик Б: найди каталог"
        assert coverage["not_found"][0]["reason"] == "no_grant"

        from app.db.models import WorkAcceptanceCriterion

        coverage_criterion = (
            await db.execute(
                select(WorkAcceptanceCriterion).where(
                    WorkAcceptanceCriterion.work_order_id == parent_id,
                    WorkAcceptanceCriterion.criterion_key == "coverage_report",
                )
            )
        ).scalar_one()
        assert coverage_criterion.status == "passed"


@pytest.mark.asyncio
async def test_promote_waiting_parents_fails_when_every_child_fails(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    # _execute_decompose opens its own session via app.db.session._get_session_factory
    # (production code, cached engine pointed at settings.database_url) — force it onto
    # this test's NullPool test_engine instead, or it can hand out a connection from a
    # different, possibly-dirty pooled engine ("another operation is in progress").
    import app.db.session as _db_session_module
    monkeypatch.setattr(_db_session_module, "_get_session_factory", lambda: factory)
    # max_replans: 0 so the single failure goes straight to "blocked" (a
    # terminal status) instead of "replanning" (active — promote_waiting_
    # parents would correctly keep waiting through a replan, which is a
    # different scenario from "every child is genuinely done, all failed").
    children_input = {
        "children": [{"objective": "Doomed child", "budgets": {"max_replans": 0}}]
    }
    async with factory() as db:
        parent = await create_work_order(db, owner_key="tester", objective="Parent")
        await create_single_step_plan(
            db, parent, kind="decompose", title="Fan out", input_data=children_input,
        )
        parent_id = parent.id
        await db.commit()

    output = await _execute_decompose(parent_id, children_input)

    async with factory() as db:
        parent = await db.get(WorkOrder, parent_id)
        await transition_work_order(db, parent, "running", actor="test")
        await enter_waiting_for_children(db, order=parent, actor="test")
        child = (
            await db.execute(
                select(WorkOrder).where(WorkOrder.id == output["child_order_ids"][0])
            )
        ).scalar_one()
        claimed = await claim_ready_step(db, worker_id="w", work_order_id=child.id)
        assert claimed is not None
        c_order, c_step, c_attempt = claimed
        await fail_attempt(
            db, order=c_order, step=c_step, attempt=c_attempt,
            error={"code": "boom"}, retryable=False, actor="w",
        )
        await db.commit()
        assert child.status == "blocked"  # terminal, no replans configured

        promoted = await promote_waiting_parents(db)
        await db.commit()

    async with factory() as db:
        parent = await db.get(WorkOrder, parent_id)
        assert promoted == 1
        # Routed through the same verify_nonempty_result every WorkOrder
        # completes through (see promote_waiting_parents docstring) — zero
        # successful children fails its default "result_present" criterion,
        # landing on the same "blocked" + verification_failed outcome a
        # normal step with an empty/errored result would get, not a
        # decompose-specific status.
        assert parent.status == "blocked"
        assert parent.blocker["code"] == "verification_failed"
