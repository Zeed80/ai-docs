"""R0 employee-eval foundation over isolated Postgres and the durable worker."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.evals import employee_harness as harness_module
from app.ai.evals.employee_cases import (
    EmployeeEvalCase,
    live_model_results,
    load_employee_case_corpus,
)
from app.ai.evals.employee_harness import (
    EmployeeDispatchResult,
    EmployeeEvalHarness,
)
from app.db.agent_runtime_models import DurableChatRun
from app.db.models import ChatMessage, ChatSession, Document, WorkOrder

_CORPUS_PATH = (
    Path(__file__).parents[1] / "app" / "ai" / "evals" / "data" / "employee_initial_v1.yaml"
)


class _TestRuntimeAdapter:
    """Serialized test-only injection into the real dispatcher and worker."""

    test_only = True
    _global_patch_lock = asyncio.Lock()

    def __init__(self) -> None:
        self.dispatch_calls = 0
        self.worker_calls = 0

    async def dispatch(
        self,
        *,
        step_id,
        attempt_id,
        session_factory,
        agent_factory,
    ) -> EmployeeDispatchResult:
        if os.environ.get("APP_ENV") != "test":
            raise RuntimeError("Employee fake runtime adapter is test-only")
        from app.tasks import durable_chat, work_orders

        async with type(self)._global_patch_lock:
            self.dispatch_calls += 1
            original_worker = durable_chat.run_durable_chat

            async def injected_worker(
                work_order_id,
                runtime_step_id,
                runtime_attempt_id,
                *,
                session_factory,
                agent_factory=None,
            ):
                self.worker_calls += 1
                return await original_worker(
                    work_order_id,
                    runtime_step_id,
                    runtime_attempt_id,
                    session_factory=session_factory,
                    agent_factory=agent_factory_from_harness,
                )

            agent_factory_from_harness = agent_factory

            def verification_not_dispatched(*args, **kwargs):
                return None

            # execute_claimed_step imports run_durable_chat at dispatch time.
            # The lock covers the complete patch lifetime and both attributes
            # are restored by the context managers even if the worker fails.
            with (
                patch.object(durable_chat, "run_durable_chat", injected_worker),
                patch.object(
                    work_orders.verify_work_step,
                    "apply_async",
                    verification_not_dispatched,
                ),
            ):
                settled = await work_orders.execute_claimed_step(
                    step_id,
                    attempt_id,
                    schedule_verification=True,
                    session_factory=session_factory,
                )
        return EmployeeDispatchResult(settled=settled, verification_dispatched=False)


def _cases():
    corpus = load_employee_case_corpus(_CORPUS_PATH)
    return corpus, {case.id: case for case in corpus.cases}


def _harness(test_engine):
    corpus, cases = _cases()
    adapter = _TestRuntimeAdapter()
    runner = EmployeeEvalHarness(
        async_sessionmaker(test_engine, expire_on_commit=False),
        runtime_adapter=adapter,
        fixture_version=corpus.fixture_version,
        code_revision="test-revision-77011c77",
        test_world_confirmation="isolated_test_database",
    )
    return runner, adapter, cases


def test_employee_case_schema_rejects_text_only_acceptance_and_unknown_predicate(tmp_path):
    _, cases = _cases()
    raw = cases["data-read-persisted-record"].model_dump(mode="json")

    for predicate in (
        {"type": "text_contains", "value": "completed"},
        {"type": "unknown_domain_check"},
    ):
        invalid = {**raw, "acceptance_predicates": [predicate]}
        with pytest.raises(ValidationError):
            EmployeeEvalCase.model_validate(invalid)

    with pytest.raises(ValidationError):
        EmployeeEvalCase.model_validate({**raw, "expected_contains": ["completed"]})

    invalid_corpus = tmp_path / "callback.yaml"
    invalid_corpus.write_text(
        "schema_version: employee_case_corpus_v1\n"
        "fixture_version: employee_initial_v1\n"
        "cases: !!python/object/apply:builtins.list []\n",
        encoding="utf-8",
    )
    with pytest.raises(yaml.constructor.ConstructorError):
        load_employee_case_corpus(invalid_corpus)


@pytest.mark.asyncio
async def test_employee_runner_rejects_non_test_world_before_opening_session(monkeypatch):
    _, cases = _cases()

    class NeverSessionFactory:
        kw = {}

        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            raise AssertionError("session factory must not be called")

    class NeverRuntimeAdapter:
        test_only = True

        async def dispatch(self, **kwargs):
            raise AssertionError("runtime adapter must not be called")

    session_factory = NeverSessionFactory()
    runner = EmployeeEvalHarness(
        session_factory,
        runtime_adapter=NeverRuntimeAdapter(),
        fixture_version="employee_initial_v1",
        code_revision="test-revision-77011c77",
        test_world_confirmation="isolated_test_database",
    )
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(RuntimeError, match="APP_ENV=test"):
        await runner.run_case(cases["data-read-persisted-record"])
    assert session_factory.calls == 0

    session_factory.kw = {
        "bind": SimpleNamespace(url=SimpleNamespace(database="employee_eval_test"))
    }
    monkeypatch.setenv("APP_ENV", "test")
    case_raw = cases["data-read-persisted-record"].model_dump(mode="json")
    unsupported_grant = EmployeeEvalCase.model_validate(
        {
            **case_raw,
            "grants": [{"capability": "employee_eval_recipient", "actions": ["read"]}],
        }
    )
    with pytest.raises(RuntimeError, match="grants must be empty"):
        await runner.run_case(unsupported_grant)
    unsupported_role = EmployeeEvalCase.model_validate(
        {**case_raw, "roles": [{"id": "unreviewed_role"}]}
    )
    with pytest.raises(RuntimeError, match="reviewed data_reader"):
        await runner.run_case(unsupported_role)
    assert session_factory.calls == 0


@pytest.mark.asyncio
async def test_employee_runner_dispatches_via_common_intake_and_durable_worker(
    test_engine, monkeypatch
):
    runner, adapter, cases = _harness(test_engine)
    calls = 0
    original_intake = harness_module.submit_agent_intake

    async def observed_intake(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original_intake(*args, **kwargs)

    monkeypatch.setattr(harness_module, "submit_agent_intake", observed_intake)
    result = await runner.run_case(cases["data-read-persisted-record"])

    assert calls == 1
    assert adapter.dispatch_calls == 1
    assert adapter.worker_calls == 1
    assert result.runtime_status.dispatcher_settled is True
    assert result.runtime_status.step_status == "succeeded"
    assert result.runtime_status.attempt_status == "succeeded"
    assert result.runtime_status.tool_call_status == "succeeded"
    assert result.runtime_status.verification_dispatched is False


@pytest.mark.asyncio
async def test_employee_runner_pass_case_reads_persisted_domain_outcome(test_engine, tmp_path):
    runner, _, cases = _harness(test_engine)
    result_file = tmp_path / "result.json"
    result = await runner.run_case(cases["data-read-persisted-record"], result_path=result_file)

    read_verdict = next(
        item
        for item in result.predicate_verdicts
        if item.predicate["type"] == "recipient_read_observed"
    )
    assert result.status == "passed"
    assert read_verdict.passed is True
    assert read_verdict.evidence["matching_persisted_reads"] == 1
    assert len(read_verdict.evidence["expected_value_digest"]) == 64
    assert all(item.observed_count == 0 for item in result.forbidden_counters)
    assert result.budget_usage.fake_model_invocations == 1
    assert result.budget_usage.physical_llm_calls == 0
    assert result.budget_usage.physical_tool_attempts == 0
    assert result.budget_usage.reported_tokens is None
    assert result.cleanup_performed is True
    assert '"runtime_mode": "fake"' in result_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_employee_runner_fail_case_reports_observed_forbidden_effect(test_engine):
    runner, _, cases = _harness(test_engine)
    result = await runner.run_case(cases["data-forbidden-recipient-write"])

    assert all(item.passed for item in result.predicate_verdicts)
    assert result.forbidden_counters[0].observed_count == 1
    assert result.forbidden_counters[0].passed is False
    unexpected = next(
        item for item in result.forbidden_counters if item.effect["type"] == "unexpected_effect"
    )
    assert unexpected.observed_count == 1
    assert unexpected.evidence["unexpected_effect_counts"] == {"recipient_write": 1}
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_employee_runner_isolates_two_runs_of_the_same_case(test_engine):
    runner, _, cases = _harness(test_engine)
    case = cases["data-read-persisted-record"]
    first = await runner.run_case(case)
    second = await runner.run_case(case)

    assert first.namespace_id != second.namespace_id
    assert first.owner_key != second.owner_key
    assert first.run_id != second.run_id
    assert first.work_order_id != second.work_order_id
    assert set(first.fixture_ids.values()).isdisjoint(second.fixture_ids.values())
    assert {entry.event_id for entry in first.trace_reference.entries}.isdisjoint(
        entry.event_id for entry in second.trace_reference.entries
    )
    assert (
        next(
            item
            for item in first.predicate_verdicts
            if item.predicate["type"] == "recipient_read_observed"
        ).evidence["matching_persisted_reads"]
        == 1
    )
    assert (
        next(
            item
            for item in second.predicate_verdicts
            if item.predicate["type"] == "recipient_read_observed"
        ).evidence["matching_persisted_reads"]
        == 1
    )


@pytest.mark.asyncio
async def test_employee_runner_cleanup_does_not_delete_foreign_fixture_ids(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    foreign_id = uuid.uuid4()
    async with factory() as db:
        db.add(
            Document(
                id=foreign_id,
                owner_sub="foreign-owner",
                file_name="foreign.json",
                file_hash="f" * 64,
                file_size=2,
                mime_type="application/json",
                storage_path="foreign/fixture",
                metadata_={"employee_eval_namespace": "foreign"},
            )
        )
        await db.commit()

    try:
        runner, _, cases = _harness(test_engine)
        result = await runner.run_case(cases["data-read-persisted-record"])
        async with factory() as db:
            assert await db.get(Document, foreign_id) is not None
            assert await db.get(WorkOrder, uuid.UUID(result.work_order_id)) is None
            for fixture_id in result.fixture_ids.values():
                assert await db.get(Document, uuid.UUID(fixture_id)) is None
    finally:
        async with factory() as db:
            foreign = await db.scalar(
                select(Document).where(
                    Document.id == foreign_id,
                    Document.owner_sub == "foreign-owner",
                )
            )
            if foreign is not None:
                await db.delete(foreign)
                await db.commit()


@pytest.mark.asyncio
async def test_employee_fake_result_is_excluded_from_live_model_metrics(test_engine):
    runner, _, cases = _harness(test_engine)
    result = await runner.run_case(cases["data-read-persisted-record"])

    assert result.runtime_mode == "fake"
    assert result.live_model_metrics_eligible is False
    assert live_model_results([result]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["fixture", "intake", "dispatch"])
async def test_employee_runner_cleans_only_owned_rows_after_phase_failure(
    test_engine, monkeypatch, failure_phase
):
    runner, adapter, cases = _harness(test_engine)
    case = cases["data-read-persisted-record"]
    captured = {}

    if failure_phase in {"fixture", "intake"}:
        original_create = runner._create_fixtures

        async def observed_create(case, world):
            captured["world"] = world
            await original_create(case, world)
            if failure_phase == "fixture":
                raise RuntimeError("fixture phase failed after commit")

        monkeypatch.setattr(runner, "_create_fixtures", observed_create)

    if failure_phase == "intake":

        async def failed_intake(*args, **kwargs):
            raise RuntimeError("intake phase failed")

        monkeypatch.setattr(harness_module, "submit_agent_intake", failed_intake)

    if failure_phase == "dispatch":
        original_submit = runner._submit_and_claim

        async def observed_submit(case, world):
            captured["world"] = world
            await original_submit(case, world)

        monkeypatch.setattr(runner, "_submit_and_claim", observed_submit)

        class FailAfterDispatch:
            test_only = True

            async def dispatch(self, **kwargs):
                await adapter.dispatch(**kwargs)
                raise RuntimeError("dispatch failed after worker commit")

        runner._runtime_adapter = FailAfterDispatch()

    with pytest.raises(RuntimeError, match="phase failed|dispatch failed"):
        await runner.run_case(case)

    world = captured["world"]
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        for fixture_id in world.fixture_ids.values():
            assert await db.get(Document, fixture_id) is None
        if world.run_id is not None:
            assert await db.get(DurableChatRun, world.run_id) is None
        if world.work_order_id is not None:
            assert await db.get(WorkOrder, world.work_order_id) is None
        if world.session_id is not None:
            assert await db.get(ChatSession, world.session_id) is None
        for message_id in (world.user_message_id, world.result_message_id):
            if message_id is not None:
                assert await db.get(ChatMessage, message_id) is None
