"""Ф1.A/Ф1.D exploratory WorkOrder mode (AGENT_AUTONOMY_ROADMAP.md) — the mode
flag, honest-coverage acceptance criteria, and the planner's exploratory
prompt addendum. No FSM/verdict-path changes: honest coverage is expressed as
ordinary criteria going through the *existing* verify_nonempty_result /
verify_semantic_criteria machinery (see test_work_order_verifier.py for that
machinery's own tests) — this file covers what's new on top of it.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Notification, WorkAcceptanceCriterion, WorkOrder, WorkStep
from app.domain.work_orders import (
    claim_ready_step,
    complete_attempt,
    create_single_step_plan,
    create_work_order,
    exploratory_acceptance_criteria,
    is_exploratory,
    transition_work_order,
    verify_nonempty_result,
)
from app.domain.work_planning import (
    _MAX_CONSECUTIVE_PLANNER_FALLBACKS,
    _summarize_step_output,
    generate_capability_plan,
    plan_work_order,
)


async def _coverage_criterion(db: AsyncSession, order_id: uuid.UUID) -> WorkAcceptanceCriterion:
    return (
        await db.execute(
            select(WorkAcceptanceCriterion).where(
                WorkAcceptanceCriterion.work_order_id == order_id,
                WorkAcceptanceCriterion.criterion_key == "coverage_report",
            )
        )
    ).scalar_one()


# ── is_exploratory ───────────────────────────────────────────────────────


class TestIsExploratory:
    def test_true_when_mode_constraint_set(self):
        order = WorkOrder(constraints={"mode": "exploratory"})
        assert is_exploratory(order) is True

    def test_false_when_no_constraints(self):
        order = WorkOrder(constraints={})
        assert is_exploratory(order) is False

    def test_false_when_constraints_none(self):
        order = WorkOrder(constraints=None)
        assert is_exploratory(order) is False

    def test_false_for_unrelated_mode_value(self):
        order = WorkOrder(constraints={"mode": "capability"})
        assert is_exploratory(order) is False


# ── exploratory_acceptance_criteria shape ───────────────────────────────


def test_exploratory_acceptance_criteria_returns_two_required_criteria():
    criteria = exploratory_acceptance_criteria()
    assert len(criteria) == 2
    keys = {c["criterion_key"] for c in criteria}
    assert keys == {"coverage_report", "honest_not_found"}
    assert all(c["required"] for c in criteria)
    kinds = {c["criterion_key"]: c["kind"] for c in criteria}
    assert kinds["coverage_report"] == "artifact"  # deterministic
    assert kinds["honest_not_found"] == "semantic"  # independent verifier


# ── coverage_report deterministic predicate ─────────────────────────────


@pytest.mark.asyncio
async def test_coverage_report_criterion_passes_with_well_formed_report(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Найди каталоги поставщиков",
        constraints={"mode": "exploratory"},
        acceptance_criteria=exploratory_acceptance_criteria(),
    )
    _plan, step = await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Report", input_data={"prompt": "x"}
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={
            "text": "Обработано 3 поставщика: 2 покрыто, 1 не найден.",
            "coverage": {
                "covered": ["acme-tools.example"],
                "partial": ["partial-tools.example"],
                "not_found": [{"item": "ghost-tools.example", "reason": "сайт недоступен (DNS)"}],
            },
        },
        actor="w1",
    )

    # coverage_report passes deterministically; honest_not_found is semantic
    # (kind="semantic" → predicate_type not handled by verify_nonempty_result
    # → unresolved_required), so overall completion correctly stays pending
    # on the independent verifier — this test only checks the deterministic half.
    await verify_nonempty_result(db_session, order=claimed_order, step=claimed_step)
    await db_session.flush()

    coverage_criterion = await _coverage_criterion(db_session, order.id)
    assert coverage_criterion.status == "passed"
    assert claimed_order.status == "blocked"  # honest_not_found still unresolved
    assert claimed_order.blocker["code"] == "independent_verification_required"
    assert "honest_not_found" in claimed_order.blocker["criteria"]


@pytest.mark.asyncio
async def test_coverage_report_criterion_fails_when_all_three_lists_empty(db_session):
    """An empty report is not honest coverage — it's indistinguishable from
    the agent never having produced a real account at all."""
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Найди каталоги поставщиков",
        constraints={"mode": "exploratory"},
        acceptance_criteria=exploratory_acceptance_criteria(),
    )
    _plan, step = await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Report", input_data={"prompt": "x"}
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={
            "text": "Ничего не сделано.",
            "coverage": {"covered": [], "partial": [], "not_found": []},
        },
        actor="w1",
    )

    await verify_nonempty_result(db_session, order=claimed_order, step=claimed_step)
    await db_session.flush()

    # Both criteria from exploratory_acceptance_criteria() are required, and
    # verify_nonempty_result only judges the deterministic one (coverage_report)
    # — honest_not_found (semantic) is always "unresolved" through this path
    # alone, which is what actually drives the reported blocker code here
    # (independent_verification_required wins over verification_failed by the
    # existing, unmodified precedence in verify_nonempty_result — see
    # test_work_order_verifier.py for that machinery's own tests). What this
    # test actually checks is that coverage_report's *own* verdict is
    # correctly "failed" for an all-empty report.
    coverage_criterion = await _coverage_criterion(db_session, order.id)
    assert coverage_criterion.status == "failed"
    assert claimed_order.status == "blocked"


@pytest.mark.asyncio
async def test_coverage_report_criterion_fails_when_shape_is_malformed(db_session):
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Найди каталоги поставщиков",
        constraints={"mode": "exploratory"},
        acceptance_criteria=exploratory_acceptance_criteria(),
    )
    _plan, step = await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Report", input_data={"prompt": "x"}
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        # coverage is a plain string, not the expected {covered,partial,not_found} object
        output={"text": "Готово.", "coverage": "всё нашёл"},
        actor="w1",
    )

    await verify_nonempty_result(db_session, order=claimed_order, step=claimed_step)
    await db_session.flush()

    coverage_criterion = await _coverage_criterion(db_session, order.id)
    assert coverage_criterion.status == "failed"
    assert claimed_order.status == "blocked"


@pytest.mark.asyncio
async def test_coverage_report_criterion_fails_without_a_text_summary(db_session):
    """verify_nonempty_result's global has_result gate (non-empty `text`)
    still applies — a good coverage report with no human-readable summary
    doesn't skip it."""
    order = await create_work_order(
        db_session,
        owner_key="tester",
        objective="Найди каталоги поставщиков",
        constraints={"mode": "exploratory"},
        acceptance_criteria=exploratory_acceptance_criteria(),
    )
    _plan, step = await create_single_step_plan(
        db_session, order, kind="agent_turn", title="Report", input_data={"prompt": "x"}
    )
    claimed = await claim_ready_step(db_session, worker_id="w1", work_order_id=order.id)
    assert claimed is not None
    claimed_order, claimed_step, attempt = claimed
    await complete_attempt(
        db_session,
        order=claimed_order,
        step=claimed_step,
        attempt=attempt,
        output={
            "text": "",
            "coverage": {"covered": ["a"], "partial": [], "not_found": []},
        },
        actor="w1",
    )

    passed = await verify_nonempty_result(db_session, order=claimed_order, step=claimed_step)
    await db_session.flush()

    assert passed is False
    assert claimed_order.status != "completed"


# ── planner prompt addendum ──────────────────────────────────────────────


class TestExploratoryPlannerPrompt:
    @pytest.mark.asyncio
    async def test_exploratory_order_gets_exploratory_guidance_in_system_prompt(self):
        order = WorkOrder(
            objective="Найди каталоги поставщиков",
            description=None,
            constraints={"mode": "exploratory"},
            budgets={},
            metadata_={},
        )
        captured: dict = {}

        async def fake_generate_json(prompt, *, system, **kwargs):
            captured["system"] = system
            return {"assumptions": [], "steps": [
                {"step_key": "s1", "title": "t", "kind": "agent_turn", "input": {}}
            ], "verification_plan": {}}

        with (
            patch("app.ai.ollama_client.generate_json", new=AsyncMock(side_effect=fake_generate_json)),
            patch(
                "app.ai.model_resolver.get_reasoning_model",
                return_value=type("M", (), {"model": "m", "provider": "ollama"})(),
            ),
            # Ф5: find_connector_hints already fails closed to [] on any error
            # (never raises), but this test is about the prompt's own
            # guidance text, not connector retrieval — stub it out so the
            # test doesn't depend on real Qdrant/embedding infra being up.
            patch("app.ai.connectors.find_connector_hints", new=AsyncMock(return_value=[])),
        ):
            await generate_capability_plan(order)

        assert "exploratory" in captured["system"].lower()
        assert "decompose" in captured["system"].lower()
        assert "not_found" in captured["system"]
        # Ф4 regression (user feedback 2026-08-20): the prompt used to end with
        # "Reporting a genuine gap in not_found is success, not failure" —
        # telling the model conceding was as good as succeeding. Must be gone,
        # replaced with an explicit persistence requirement.
        assert "success, not failure" not in captured["system"]
        assert "actually complete the objective" in captured["system"]

    @pytest.mark.asyncio
    async def test_exploratory_order_prompt_includes_connector_hints(self):
        """Ф5 (AGENT_AUTONOMY_ROADMAP.md): the planner prompt's JSON payload
        carries whatever find_connector_hints returns for the objective, so a
        matching self-learned strategy is visible to the model."""
        order = WorkOrder(
            objective="Найди каталог поставщика Haltec",
            description=None,
            constraints={"mode": "exploratory"},
            budgets={},
            metadata_={},
        )
        captured: dict = {}

        async def fake_generate_json(prompt, *, system, **kwargs):
            captured["prompt"] = prompt
            return {"assumptions": [], "steps": [
                {"step_key": "s1", "title": "t", "kind": "agent_turn", "input": {}}
            ], "verification_plan": {}}

        hint = {
            "domain_pattern": "haltec.ru",
            "strategy": {"queries": ["haltec сверла каталог"], "sample_url": "https://haltec.ru/catalog"},
            "status": "active",
            "score": 0.81,
        }
        with (
            patch("app.ai.ollama_client.generate_json", new=AsyncMock(side_effect=fake_generate_json)),
            patch(
                "app.ai.model_resolver.get_reasoning_model",
                return_value=type("M", (), {"model": "m", "provider": "ollama"})(),
            ),
            patch("app.ai.connectors.find_connector_hints", new=AsyncMock(return_value=[hint])),
        ):
            await generate_capability_plan(order)

        assert "haltec.ru" in captured["prompt"]
        assert "connector_hints" in captured["prompt"]

    @pytest.mark.asyncio
    async def test_non_exploratory_order_gets_plain_system_prompt(self):
        order = WorkOrder(
            objective="Одобрить накладную №1",
            description=None,
            constraints={},
            budgets={},
            metadata_={},
        )
        captured: dict = {}

        async def fake_generate_json(prompt, *, system, **kwargs):
            captured["system"] = system
            return {"assumptions": [], "steps": [
                {"step_key": "s1", "title": "t", "kind": "agent_turn", "input": {}}
            ], "verification_plan": {}}

        with (
            patch("app.ai.ollama_client.generate_json", new=AsyncMock(side_effect=fake_generate_json)),
            patch(
                "app.ai.model_resolver.get_reasoning_model",
                return_value=type("M", (), {"model": "m", "provider": "ollama"})(),
            ),
        ):
            await generate_capability_plan(order)

        assert "exploratory" not in captured["system"].lower()


# ── Ф4: progress notifications for long-running exploratory WorkOrders ─────


class TestExploratoryProgressNotifications:
    @pytest.mark.asyncio
    async def test_exploratory_order_reaching_blocked_notifies_owner(self, db_session):
        order = await create_work_order(
            db_session,
            owner_key="alice",
            objective="Найди каталоги поставщиков",
            constraints={"mode": "exploratory"},
        )
        await create_single_step_plan(
            db_session, order, kind="agent_turn", title="x", input_data={"prompt": "x"}
        )
        await claim_ready_step(db_session, worker_id="w", work_order_id=order.id)
        order.blocker = {"code": "no_grant"}

        await transition_work_order(db_session, order, "blocked", actor="test")
        await db_session.flush()

        notif = (
            await db_session.execute(
                select(Notification).where(
                    Notification.entity_id == order.id,
                    Notification.source_task == "workorder.progress",
                )
            )
        ).scalar_one_or_none()
        assert notif is not None
        assert notif.user_sub == "alice"
        assert "no_grant" in notif.body

    @pytest.mark.asyncio
    async def test_non_exploratory_order_reaching_blocked_does_not_notify(self, db_session):
        """Regression guard: the overwhelming majority of WorkOrders are
        short capability-mode tasks — must stay exactly as noisy as before."""
        order = await create_work_order(db_session, owner_key="bob", objective="Одобрить накладную")
        await create_single_step_plan(
            db_session, order, kind="agent_turn", title="x", input_data={"prompt": "x"}
        )
        await claim_ready_step(db_session, worker_id="w", work_order_id=order.id)
        order.blocker = {"code": "boom"}

        await transition_work_order(db_session, order, "blocked", actor="test")
        await db_session.flush()

        notif = (
            await db_session.execute(select(Notification).where(Notification.entity_id == order.id))
        ).scalar_one_or_none()
        assert notif is None

    @pytest.mark.asyncio
    async def test_exploratory_order_completing_notifies_owner(self, db_session):
        order = await create_work_order(
            db_session,
            owner_key="alice",
            objective="Найди каталоги поставщиков",
            constraints={"mode": "exploratory"},
        )
        _plan, step = await create_single_step_plan(
            db_session, order, kind="agent_turn", title="x", input_data={"prompt": "x"}
        )
        claimed = await claim_ready_step(db_session, worker_id="w", work_order_id=order.id)
        _order, claimed_step, attempt = claimed
        await complete_attempt(
            db_session, order=order, step=claimed_step, attempt=attempt,
            output={"text": "готово"}, actor="w",
        )

        assert await verify_nonempty_result(db_session, order=order, step=claimed_step)
        await db_session.flush()

        notifs = (
            await db_session.execute(
                select(Notification).where(
                    Notification.entity_id == order.id,
                    Notification.source_task == "workorder.progress",
                )
            )
        ).scalars().all()
        # verify_nonempty_result transitions verifying -> completed — both
        # are notify-worthy statuses, so two notifications are expected here,
        # not a bug.
        assert len(notifs) >= 1
        assert any("заверш" in n.title.lower() for n in notifs)


# ── Ф4-re post-mortem (pilot 5db58ac6): completed_context bounding + a cap
# on the planner-schema-failure/free-form-narration fallback cascade ───────


class TestSummarizeStepOutput:
    def test_agent_turn_output_is_relabeled_not_passed_through_raw(self):
        step = WorkStep(kind="agent_turn", output={"text": "Не удалось завершить.", "executor": "agent_turn"})
        summary = _summarize_step_output(step)
        assert summary["kind"] == "agent_turn"
        assert "not a capability result" in summary["note"].lower()
        assert summary["excerpt"] == "Не удалось завершить."
        # The raw {"text": ...} shape must not appear verbatim — that shape is
        # exactly what taught the model to imitate it as a top-level plan reply.
        assert "text" not in summary

    def test_agent_turn_excerpt_is_truncated(self):
        step = WorkStep(kind="agent_turn", output={"text": "x" * 1000})
        summary = _summarize_step_output(step)
        assert len(summary["excerpt"]) == 240

    def test_capability_output_passed_through_when_small(self):
        step = WorkStep(kind="capability", output={"result": {"supplier_id": "abc"}})
        assert _summarize_step_output(step) == {"result": {"supplier_id": "abc"}}

    def test_large_capability_output_is_truncated(self):
        step = WorkStep(kind="capability", output={"text": "y" * 5000})
        summary = _summarize_step_output(step)
        assert "_truncated_output" in summary
        assert summary["_truncated_output"].endswith("...[truncated]")
        assert len(summary["_truncated_output"]) < 5000


class TestPlannerErrorFeedback:
    @pytest.mark.asyncio
    async def test_planner_error_context_reaches_prompt_and_system_when_present(self):
        order = WorkOrder(
            objective="Найди каталоги поставщиков",
            description=None,
            constraints={},
            budgets={},
            metadata_={},
        )
        captured: dict = {}

        async def fake_generate_json(prompt, *, system, **kwargs):
            captured["prompt"] = prompt
            captured["system"] = system
            return {"assumptions": [], "steps": [
                {"step_key": "s1", "title": "t", "kind": "agent_turn", "input": {}}
            ], "verification_plan": {}}

        with (
            patch("app.ai.ollama_client.generate_json", new=AsyncMock(side_effect=fake_generate_json)),
            patch(
                "app.ai.model_resolver.get_reasoning_model",
                return_value=type("M", (), {"model": "m", "provider": "ollama"})(),
            ),
        ):
            await generate_capability_plan(
                order, planner_error_context="steps: Field required"
            )

        assert "last_planner_error" in captured["prompt"]
        assert "steps: Field required" in captured["prompt"]
        assert "REJECTED" in captured["system"]
        assert "steps: Field required" in captured["system"]

    @pytest.mark.asyncio
    async def test_no_error_context_when_planner_error_context_absent(self):
        order = WorkOrder(
            objective="Найди каталоги поставщиков", description=None,
            constraints={}, budgets={}, metadata_={},
        )
        captured: dict = {}

        async def fake_generate_json(prompt, *, system, **kwargs):
            captured["system"] = system
            return {"assumptions": [], "steps": [
                {"step_key": "s1", "title": "t", "kind": "agent_turn", "input": {}}
            ], "verification_plan": {}}

        with (
            patch("app.ai.ollama_client.generate_json", new=AsyncMock(side_effect=fake_generate_json)),
            patch(
                "app.ai.model_resolver.get_reasoning_model",
                return_value=type("M", (), {"model": "m", "provider": "ollama"})(),
            ),
        ):
            await generate_capability_plan(order)

        assert "REJECTED" not in captured["system"]


class TestPlannerFallbackStreakCap:
    @pytest.mark.asyncio
    async def test_consecutive_schema_failures_below_threshold_keep_replanning(self, db_session):
        order = await create_work_order(
            db_session, owner_key="tester", objective="Найди каталоги поставщиков",
        )
        with patch(
            "app.domain.work_planning.generate_capability_plan",
            new=AsyncMock(side_effect=ValueError("planner produced an invalid capability DAG: boom")),
        ):
            for _ in range(_MAX_CONSECUTIVE_PLANNER_FALLBACKS - 1):
                order.status = "replanning"
                await plan_work_order(db_session, order, use_model=True)
                await db_session.flush()

        assert order.status != "blocked"
        assert order.metadata_["planner_fallback_streak"] == _MAX_CONSECUTIVE_PLANNER_FALLBACKS - 1

    @pytest.mark.asyncio
    async def test_reaching_threshold_blocks_with_a_distinct_honest_reason(self, db_session):
        order = await create_work_order(
            db_session, owner_key="tester", objective="Найди каталоги поставщиков",
        )
        with patch(
            "app.domain.work_planning.generate_capability_plan",
            new=AsyncMock(side_effect=ValueError("planner produced an invalid capability DAG: boom")),
        ):
            for _ in range(_MAX_CONSECUTIVE_PLANNER_FALLBACKS):
                order.status = "replanning"
                await plan_work_order(db_session, order, use_model=True)
                await db_session.flush()

        assert order.status == "blocked"
        assert order.blocker["code"] == "planner_schema_failure_streak"
        assert order.blocker["streak"] == _MAX_CONSECUTIVE_PLANNER_FALLBACKS

    @pytest.mark.asyncio
    async def test_a_successful_plan_in_between_resets_the_streak(self, db_session):
        order = await create_work_order(
            db_session, owner_key="tester", objective="Найди каталоги поставщиков",
        )
        good_plan = {"assumptions": [], "steps": [
            {"step_key": "s1", "title": "t", "kind": "agent_turn", "input": {}}
        ], "verification_plan": {}}

        with patch(
            "app.domain.work_planning.generate_capability_plan",
            new=AsyncMock(side_effect=ValueError("boom")),
        ):
            for _ in range(_MAX_CONSECUTIVE_PLANNER_FALLBACKS - 1):
                order.status = "replanning"
                await plan_work_order(db_session, order, use_model=True)
                await db_session.flush()
        assert order.metadata_["planner_fallback_streak"] == _MAX_CONSECUTIVE_PLANNER_FALLBACKS - 1

        from app.domain.work_planning import PlannedWork

        with patch(
            "app.domain.work_planning.generate_capability_plan",
            new=AsyncMock(return_value=PlannedWork.model_validate(good_plan)),
        ):
            order.status = "replanning"
            await plan_work_order(db_session, order, use_model=True)
            await db_session.flush()

        assert order.status != "blocked"
        assert "planner_fallback_streak" not in order.metadata_
        assert "last_planner_error" not in order.metadata_

        # Streak must start over, not resume from before the reset.
        with patch(
            "app.domain.work_planning.generate_capability_plan",
            new=AsyncMock(side_effect=ValueError("boom again")),
        ):
            order.status = "replanning"
            await plan_work_order(db_session, order, use_model=True)
            await db_session.flush()
        assert order.status != "blocked"
        assert order.metadata_["planner_fallback_streak"] == 1

    @pytest.mark.asyncio
    async def test_use_model_false_never_triggers_the_streak_cap(self, db_session):
        """fallback_plan() itself never raises — use_model=False (test/manual
        single-step) runs are not the schema-cascade this cap targets."""
        order = await create_work_order(
            db_session, owner_key="tester", objective="Найди каталоги поставщиков",
        )
        for _ in range(_MAX_CONSECUTIVE_PLANNER_FALLBACKS + 2):
            order.status = "replanning"
            await plan_work_order(db_session, order, use_model=False)
            await db_session.flush()

        assert order.status != "blocked"
        assert "planner_fallback_streak" not in (order.metadata_ or {})
