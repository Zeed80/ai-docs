"""Isolated outcome-eval harness over the common intake and durable runtime."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from sqlalchemy import delete, select, update

from app.ai.evals.employee_cases import (
    DomainRecordFixture,
    EmployeeBudgetUsage,
    EmployeeEvalCase,
    EmployeeEvalResult,
    ForbiddenEffectCounter,
    PredicateVerdict,
    ReadFixtureAction,
    RecipientReadObservedPredicate,
    RuntimePersistedPredicate,
    RuntimeStatusEvidence,
    TraceEntry,
    TraceReference,
    WriteRecipientAction,
)
from app.db.agent_runtime_models import DurableChatRun
from app.db.models import (
    ChatMessage,
    ChatSession,
    Document,
    WorkEvent,
    WorkOrder,
    WorkStep,
    WorkStepAttempt,
    WorkToolCall,
)
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.agent_intake import (
    AgentIntakeRequest,
    VerifiedIntakeIdentity,
    submit_agent_intake,
)
from app.domain.work_orders import append_event, claim_ready_step


@dataclass(frozen=True)
class EmployeeDispatchResult:
    settled: bool
    verification_dispatched: bool


class EmployeeRuntimeAdapter(Protocol):
    """Test-world bridge to the production dispatcher.

    The adapter is Python-side dependency injection, never YAML data. R0 tests
    use it to route the fake agent through ``run_durable_chat(agent_factory=...)``
    while keeping live providers and Celery dispatch disabled.
    """

    test_only: bool

    async def dispatch(
        self,
        *,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID,
        session_factory: Any,
        agent_factory: Callable[[Callable[..., Any]], Any],
    ) -> EmployeeDispatchResult: ...


@dataclass
class _RunWorld:
    namespace_id: str
    owner_key: str
    fixture_ids: dict[str, uuid.UUID]
    run_id: uuid.UUID | None = None
    work_order_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    user_message_id: uuid.UUID | None = None
    result_message_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    ledger_id: uuid.UUID | None = None


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


class _LocalRecipient:
    def __init__(self, *, session_factory: Any, world: _RunWorld) -> None:
        self._session_factory = session_factory
        self._world = world

    async def read_fixture(self, fixture_ref: str) -> None:
        fixture_id = self._world.fixture_ids[fixture_ref]
        async with self._session_factory() as db:
            document = await db.scalar(
                select(Document).where(
                    Document.id == fixture_id,
                    Document.owner_sub == self._world.owner_key,
                )
            )
            metadata = document.metadata_ if document is not None else None
            if (
                document is None
                or not isinstance(metadata, dict)
                or metadata.get("employee_eval_namespace") != self._world.namespace_id
                or metadata.get("fixture_ref") != fixture_ref
            ):
                raise RuntimeError("Owned employee-eval fixture is unavailable")
            order = await db.scalar(
                select(WorkOrder).where(
                    WorkOrder.id == self._world.work_order_id,
                    WorkOrder.owner_key == self._world.owner_key,
                )
            )
            if order is None:
                raise RuntimeError("Owned employee-eval WorkOrder is unavailable")
            await append_event(
                db,
                order.id,
                "employee_eval.recipient_read",
                actor="employee-eval-recipient",
                payload={
                    "namespace_id": self._world.namespace_id,
                    "fixture_ref": fixture_ref,
                    "fixture_id": str(fixture_id),
                    "value_digest": _digest(metadata["value"]),
                },
            )
            await db.commit()

    async def write(self, key: str, value: dict[str, Any]) -> None:
        async with self._session_factory() as db:
            order = await db.scalar(
                select(WorkOrder).where(
                    WorkOrder.id == self._world.work_order_id,
                    WorkOrder.owner_key == self._world.owner_key,
                )
            )
            if order is None:
                raise RuntimeError("Owned employee-eval WorkOrder is unavailable")
            await append_event(
                db,
                order.id,
                "employee_eval.recipient_write",
                actor="employee-eval-recipient",
                payload={
                    "namespace_id": self._world.namespace_id,
                    "key": key,
                    "value_digest": _digest(value),
                },
            )
            await db.commit()


class EmployeeEvalHarness:
    """Run deterministic fake-model cases without claiming live-model quality."""

    def __init__(
        self,
        session_factory: Any,
        *,
        runtime_adapter: EmployeeRuntimeAdapter,
        fixture_version: str,
        code_revision: str,
        test_world_confirmation: str,
        fake_model_version: str = "employee-scripted-fake-v1",
        config_version: str = "employee-harness-v1",
    ) -> None:
        self._session_factory = session_factory
        self._runtime_adapter = runtime_adapter
        self.fixture_version = fixture_version
        self.code_revision = code_revision
        self.fake_model_version = fake_model_version
        self.config_version = config_version
        self.test_world_confirmation = test_world_confirmation

    def _assert_isolated_test_world(self) -> None:
        """Reject before opening a session or creating a fixture."""
        if os.environ.get("APP_ENV") != "test":
            raise RuntimeError("Employee eval harness requires APP_ENV=test")
        if self.test_world_confirmation != "isolated_test_database":
            raise RuntimeError(
                "Employee eval harness requires explicit isolated test DB confirmation"
            )
        if getattr(self._runtime_adapter, "test_only", False) is not True:
            raise RuntimeError("Employee eval harness requires a test-only runtime adapter")
        bind = getattr(self._session_factory, "kw", {}).get("bind")
        database_name = getattr(getattr(bind, "url", None), "database", None)
        if not database_name or "test" not in str(database_name).casefold():
            raise RuntimeError("Employee eval harness refuses a database not named as a test DB")

    @staticmethod
    def _assert_supported_case(case: EmployeeEvalCase) -> None:
        """Do not imply R0 configured production RBAC/grants that it did not."""
        if [role.id for role in case.roles] != ["data_reader"]:
            raise RuntimeError("R0 supports only the reviewed data_reader eval role")
        if case.grants:
            raise RuntimeError("R0 does not configure runtime grants; grants must be empty")

    async def run_case(
        self,
        case: EmployeeEvalCase,
        *,
        cleanup: bool = True,
        result_path: str | Path | None = None,
    ) -> EmployeeEvalResult:
        self._assert_isolated_test_world()
        self._assert_supported_case(case)
        namespace_id = str(uuid.uuid4())
        world = _RunWorld(
            namespace_id=namespace_id,
            owner_key=f"employee-eval:{case.owner.key}:{namespace_id}",
            fixture_ids={},
        )
        fake_state = SimpleNamespace(invocations=0)
        try:
            await self._create_fixtures(case, world)
            await self._submit_and_claim(case, world)
            recipient = _LocalRecipient(session_factory=self._session_factory, world=world)
            agent_factory = self._agent_factory(case, recipient, fake_state)
            dispatch = await self._runtime_adapter.dispatch(
                step_id=world.step_id,
                attempt_id=world.attempt_id,
                session_factory=self._session_factory,
                agent_factory=agent_factory,
            )
            result = await self._build_result(case, world, dispatch, fake_state.invocations)
            if cleanup:
                counts = await self._cleanup(world)
                result = result.model_copy(
                    update={"cleanup_performed": True, "cleanup_counts": counts}
                )
            if result_path is not None:
                Path(result_path).write_text(result.model_dump_json(indent=2), encoding="utf-8")
            return result
        except BaseException:
            await self._cleanup(world)
            raise

    async def _create_fixtures(self, case: EmployeeEvalCase, world: _RunWorld) -> None:
        async with self._session_factory() as db:
            for fixture in case.fixtures:
                if not isinstance(fixture, DomainRecordFixture):
                    raise TypeError(f"Unsupported fixture model: {type(fixture).__name__}")
                document = Document(
                    owner_sub=world.owner_key,
                    file_name=f"{fixture.ref}.json",
                    file_hash=_digest(fixture.value),
                    file_size=len(json.dumps(fixture.value, ensure_ascii=False).encode()),
                    mime_type="application/json",
                    storage_path=f"employee-eval/{world.namespace_id}/{fixture.ref}",
                    metadata_={
                        "employee_eval_namespace": world.namespace_id,
                        "fixture_ref": fixture.ref,
                        "value": fixture.value,
                    },
                )
                db.add(document)
                await db.flush()
                world.fixture_ids[fixture.ref] = document.id
            await db.commit()

    async def _submit_and_claim(self, case: EmployeeEvalCase, world: _RunWorld) -> None:
        async with self._session_factory() as db:
            intake = await submit_agent_intake(
                db,
                identity=VerifiedIntakeIdentity(
                    account_key=world.owner_key,
                    channel="employee_eval",
                ),
                request=AgentIntakeRequest(
                    channel="employee_eval",
                    external_message_id=f"case:{case.id}:{world.namespace_id}",
                    request_id=uuid.uuid4(),
                    content=case.task,
                    workspace_context={
                        "employee_eval_namespace": world.namespace_id,
                        "case_id": case.id,
                    },
                ),
            )
            world.run_id = intake.run.id
            world.work_order_id = intake.order.id
            world.session_id = intake.run.session_id
            world.user_message_id = intake.run.user_message_id
            world.ledger_id = intake.order.budget_ledger_id

        async with self._session_factory() as db:
            order = await db.scalar(
                select(WorkOrder).where(
                    WorkOrder.id == world.work_order_id,
                    WorkOrder.owner_key == world.owner_key,
                )
            )
            ledger = await db.scalar(
                select(WorkBudgetLedger).where(
                    WorkBudgetLedger.id == world.ledger_id,
                    WorkBudgetLedger.root_work_order_id == world.work_order_id,
                    WorkBudgetLedger.owner_key == world.owner_key,
                )
            )
            if order is None or ledger is None:
                raise RuntimeError("Employee-eval intake budget binding is missing")
            order.budgets = {
                "max_active_seconds": case.budget.max_active_seconds,
                "max_tool_attempts": case.budget.max_tool_attempts,
                "max_llm_calls": case.budget.max_llm_calls,
                "max_replans": case.budget.max_replans,
            }
            ledger.max_active_seconds = case.budget.max_active_seconds
            ledger.max_tool_attempts = case.budget.max_tool_attempts
            ledger.max_llm_calls = case.budget.max_llm_calls
            ledger.max_replans = case.budget.max_replans
            claimed = await claim_ready_step(
                db,
                worker_id=f"employee-eval:{world.namespace_id}",
                work_order_id=world.work_order_id,
            )
            if claimed is None:
                raise RuntimeError("Employee-eval WorkOrder was not claimable")
            _, step, attempt = claimed
            world.step_id = step.id
            world.attempt_id = attempt.id
            await db.commit()

    def _agent_factory(
        self,
        case: EmployeeEvalCase,
        recipient: _LocalRecipient,
        fake_state: SimpleNamespace,
    ) -> Callable[[Callable[..., Any]], Any]:
        max_invocations = case.budget.max_fake_model_invocations

        class ScriptedFakeAgent:
            def __init__(self, send: Callable[..., Any]) -> None:
                self.send = send
                self._executor = self
                self.total_tokens = None
                self._work_budget_context = None

            def set_work_budget_context(self, context: Any) -> None:
                self._work_budget_context = context

            def hydrate_history(self, history: list[dict[str, Any]]) -> None:
                self.history = history

            async def on_user_message(self, prompt: str, **kwargs: Any) -> None:
                fake_state.invocations += 1
                if fake_state.invocations > max_invocations:
                    raise RuntimeError("Fake-model invocation budget exceeded")
                for index, action in enumerate(case.initial_state.model_actions, start=1):
                    await self.send(
                        {
                            "type": "tool_call",
                            "tool": f"employee_eval.{action.type}",
                            "args": {"script_index": index},
                        }
                    )
                    if isinstance(action, ReadFixtureAction):
                        await recipient.read_fixture(action.fixture_ref)
                    elif isinstance(action, WriteRecipientAction):
                        await recipient.write(action.key, action.value)
                    else:  # pragma: no cover - discriminated schema is closed
                        raise TypeError(f"Unsupported fake action: {type(action).__name__}")
                await self.send({"type": "text", "content": "Scripted fake runtime completed."})
                await self.send({"type": "done"})

        return ScriptedFakeAgent

    async def _owned_events(self, world: _RunWorld) -> list[WorkEvent]:
        async with self._session_factory() as db:
            return list(
                await db.scalars(
                    select(WorkEvent)
                    .join(WorkOrder, WorkOrder.id == WorkEvent.work_order_id)
                    .where(
                        WorkEvent.work_order_id == world.work_order_id,
                        WorkOrder.id == world.work_order_id,
                        WorkOrder.owner_key == world.owner_key,
                    )
                    .order_by(WorkEvent.sequence)
                )
            )

    async def _build_result(
        self,
        case: EmployeeEvalCase,
        world: _RunWorld,
        dispatch: EmployeeDispatchResult,
        fake_invocations: int,
    ) -> EmployeeEvalResult:
        events = await self._owned_events(world)
        event_payloads = [
            event
            for event in events
            if (event.payload or {}).get("namespace_id") == world.namespace_id
        ]
        fixture_by_ref = {fixture.ref: fixture for fixture in case.fixtures}
        verdicts: list[PredicateVerdict] = []
        runtime_status: RuntimeStatusEvidence | None = None

        async with self._session_factory() as db:
            order = await db.scalar(
                select(WorkOrder).where(
                    WorkOrder.id == world.work_order_id,
                    WorkOrder.owner_key == world.owner_key,
                )
            )
            run = await db.scalar(
                select(DurableChatRun).where(
                    DurableChatRun.id == world.run_id,
                    DurableChatRun.work_order_id == world.work_order_id,
                    DurableChatRun.owner_key == world.owner_key,
                )
            )
            step = await db.scalar(
                select(WorkStep)
                .join(WorkOrder, WorkOrder.id == WorkStep.work_order_id)
                .where(
                    WorkStep.id == world.step_id,
                    WorkStep.work_order_id == world.work_order_id,
                    WorkOrder.owner_key == world.owner_key,
                )
            )
            attempt = await db.scalar(
                select(WorkStepAttempt)
                .join(WorkStep, WorkStep.id == WorkStepAttempt.step_id)
                .join(WorkOrder, WorkOrder.id == WorkStep.work_order_id)
                .where(
                    WorkStepAttempt.id == world.attempt_id,
                    WorkStepAttempt.step_id == world.step_id,
                    WorkOrder.id == world.work_order_id,
                    WorkOrder.owner_key == world.owner_key,
                )
            )
            call = await db.scalar(
                select(WorkToolCall)
                .join(WorkOrder, WorkOrder.id == WorkToolCall.work_order_id)
                .where(
                    WorkToolCall.work_order_id == world.work_order_id,
                    WorkToolCall.step_id == world.step_id,
                    WorkToolCall.attempt_id == world.attempt_id,
                    WorkOrder.owner_key == world.owner_key,
                )
            )
            result_message = None
            if run is not None and run.result_message_id is not None:
                result_message = await db.scalar(
                    select(ChatMessage)
                    .join(ChatSession, ChatSession.id == ChatMessage.session_id)
                    .where(
                        ChatMessage.id == run.result_message_id,
                        ChatMessage.session_id == world.session_id,
                        ChatSession.id == world.session_id,
                        ChatSession.user_key == world.owner_key,
                    )
                )
                world.result_message_id = run.result_message_id
            runtime_status = RuntimeStatusEvidence(
                dispatcher_settled=dispatch.settled,
                order_status=order.status if order is not None else "missing",
                step_status=step.state if step is not None else "missing",
                attempt_status=attempt.status if attempt is not None else "missing",
                tool_call_status=call.status if call is not None else "missing",
                verification_dispatched=dispatch.verification_dispatched,
            )
            runtime_ok = bool(
                dispatch.settled
                and order is not None
                and run is not None
                and step is not None
                and step.state == "succeeded"
                and attempt is not None
                and attempt.status == "succeeded"
                and call is not None
                and call.status == "succeeded"
                and result_message is not None
            )

            for predicate in case.acceptance_predicates:
                if isinstance(predicate, RuntimePersistedPredicate):
                    verdicts.append(
                        PredicateVerdict(
                            predicate=predicate.model_dump(mode="json"),
                            passed=runtime_ok,
                            evidence={
                                "run_persisted": run is not None,
                                "result_message_persisted": result_message is not None,
                                "order_status": runtime_status.order_status,
                                "step_status": runtime_status.step_status,
                                "attempt_status": runtime_status.attempt_status,
                                "tool_call_status": runtime_status.tool_call_status,
                                "work_order_completed_claimed": False,
                            },
                        )
                    )
                elif isinstance(predicate, RecipientReadObservedPredicate):
                    fixture = fixture_by_ref[predicate.fixture_ref]
                    fixture_id = world.fixture_ids[predicate.fixture_ref]
                    matching = [
                        event
                        for event in event_payloads
                        if event.event_type == "employee_eval.recipient_read"
                        and event.payload.get("fixture_ref") == predicate.fixture_ref
                        and event.payload.get("fixture_id") == str(fixture_id)
                        and event.payload.get("value_digest") == _digest(fixture.value)
                    ]
                    verdicts.append(
                        PredicateVerdict(
                            predicate=predicate.model_dump(mode="json"),
                            passed=len(matching) == 1,
                            evidence={
                                "matching_persisted_reads": len(matching),
                                "fixture_id": str(fixture_id),
                                "expected_value_digest": _digest(fixture.value),
                            },
                        )
                    )
                else:  # pragma: no cover - discriminated schema is closed
                    raise TypeError(f"Unsupported predicate: {type(predicate).__name__}")

            ledger = await db.scalar(
                select(WorkBudgetLedger).where(
                    WorkBudgetLedger.id == world.ledger_id,
                    WorkBudgetLedger.root_work_order_id == world.work_order_id,
                    WorkBudgetLedger.owner_key == world.owner_key,
                )
            )
            reservations = []
            if ledger is not None:
                reservations = list(
                    await db.scalars(
                        select(WorkBudgetReservation).where(
                            WorkBudgetReservation.ledger_id == ledger.id,
                            WorkBudgetReservation.work_order_id == world.work_order_id,
                            WorkBudgetReservation.ledger_id.in_(
                                select(WorkBudgetLedger.id).where(
                                    WorkBudgetLedger.id == world.ledger_id,
                                    WorkBudgetLedger.root_work_order_id == world.work_order_id,
                                    WorkBudgetLedger.owner_key == world.owner_key,
                                )
                            ),
                        )
                    )
                )

        counters: list[ForbiddenEffectCounter] = []
        for effect in case.forbidden_effects:
            observed = sum(
                event.event_type == "employee_eval.recipient_write" for event in event_payloads
            )
            counters.append(
                ForbiddenEffectCounter(
                    effect=effect.model_dump(mode="json"),
                    observed_count=observed,
                    passed=observed <= effect.max_count,
                    evidence={
                        "event_type": "employee_eval.recipient_write",
                        "namespace_id": world.namespace_id,
                    },
                )
            )

        observed_effects: dict[str, int] = {
            "runtime_persistence": int(runtime_status.dispatcher_settled),
            "assistant_response": int(
                any(item.event_type == "chat.response_saved" for item in events)
            ),
            "recipient_read": sum(
                item.event_type == "employee_eval.recipient_read" for item in event_payloads
            ),
            "recipient_write": sum(
                item.event_type == "employee_eval.recipient_write" for item in event_payloads
            ),
        }
        unexpected = {
            effect: count
            for effect, count in observed_effects.items()
            if count and effect not in case.allowed_effects
        }
        counters.append(
            ForbiddenEffectCounter(
                effect={"type": "unexpected_effect"},
                observed_count=sum(unexpected.values()),
                passed=not unexpected,
                evidence={
                    "allowed_effects": list(case.allowed_effects),
                    "unexpected_effect_counts": unexpected,
                },
            )
        )

        execution_fences = sum(
            item.dimension == "llm_calls"
            and item.operation_key == f"execution:{world.attempt_id}"
            and float(item.reserved_units) == 0
            for item in reservations
        )
        physical_llm_calls = sum(
            item.dimension == "llm_calls"
            and float(item.actual_units if item.actual_units is not None else item.reserved_units)
            > 0
            for item in reservations
        )
        physical_tool_attempts = sum(
            item.dimension == "tool_attempts"
            and float(item.actual_units if item.actual_units is not None else item.reserved_units)
            > 0
            for item in reservations
        )
        budget_usage = EmployeeBudgetUsage(
            configured=case.budget,
            fake_model_invocations=fake_invocations,
            execution_fence_reservations=execution_fences,
            physical_llm_calls=physical_llm_calls,
            physical_tool_attempts=physical_tool_attempts,
            reported_tokens=attempt.tokens_used if attempt is not None else None,
            reported_cost_usd=attempt.cost_usd if attempt is not None else None,
        )
        passed = all(item.passed for item in verdicts) and all(item.passed for item in counters)
        config_digest = _digest(
            {
                "case": case.model_dump(mode="json"),
                "fixture_version": self.fixture_version,
                "fake_model_version": self.fake_model_version,
                "config_version": self.config_version,
            }
        )
        trace = TraceReference(
            work_order_id=str(world.work_order_id),
            entries=tuple(
                TraceEntry(
                    event_id=str(event.id),
                    sequence=event.sequence,
                    event_type=event.event_type,
                    payload_digest=_digest(event.payload),
                )
                for event in events
            ),
        )
        return EmployeeEvalResult(
            case_id=case.id,
            case_version=case.case_version,
            fixture_version=self.fixture_version,
            code_revision=self.code_revision,
            config_version=self.config_version,
            config_digest=config_digest,
            fake_model_version=self.fake_model_version,
            runtime_mode="fake",
            namespace_id=world.namespace_id,
            owner_key=world.owner_key,
            run_id=str(world.run_id),
            work_order_id=str(world.work_order_id),
            step_id=str(world.step_id),
            attempt_id=str(world.attempt_id),
            fixture_ids={key: str(value) for key, value in world.fixture_ids.items()},
            predicate_verdicts=tuple(verdicts),
            forbidden_counters=tuple(counters),
            budget_usage=budget_usage,
            runtime_status=runtime_status,
            trace_reference=trace,
            status="passed" if passed else "failed",
        )

    async def _cleanup(self, world: _RunWorld) -> dict[str, int]:
        counts: dict[str, int] = {}
        async with self._session_factory() as db:
            if world.run_id is not None and world.work_order_id is not None:
                owned_run = await db.scalar(
                    select(DurableChatRun).where(
                        DurableChatRun.id == world.run_id,
                        DurableChatRun.work_order_id == world.work_order_id,
                        DurableChatRun.owner_key == world.owner_key,
                    )
                )
                if owned_run is not None and owned_run.result_message_id is not None:
                    world.result_message_id = owned_run.result_message_id
                result = await db.execute(
                    delete(DurableChatRun).where(
                        DurableChatRun.id == world.run_id,
                        DurableChatRun.work_order_id == world.work_order_id,
                        DurableChatRun.owner_key == world.owner_key,
                    )
                )
                counts["durable_chat_runs"] = result.rowcount
            if world.ledger_id is not None and world.work_order_id is not None:
                result = await db.execute(
                    delete(WorkBudgetReservation).where(
                        WorkBudgetReservation.ledger_id == world.ledger_id,
                        WorkBudgetReservation.work_order_id == world.work_order_id,
                        WorkBudgetReservation.ledger_id.in_(
                            select(WorkBudgetLedger.id).where(
                                WorkBudgetLedger.id == world.ledger_id,
                                WorkBudgetLedger.root_work_order_id == world.work_order_id,
                                WorkBudgetLedger.owner_key == world.owner_key,
                            )
                        ),
                    )
                )
                counts["budget_reservations"] = result.rowcount
                await db.execute(
                    update(WorkOrder)
                    .where(
                        WorkOrder.id == world.work_order_id,
                        WorkOrder.owner_key == world.owner_key,
                        WorkOrder.budget_ledger_id == world.ledger_id,
                    )
                    .values(budget_ledger_id=None)
                )
                result = await db.execute(
                    delete(WorkBudgetLedger).where(
                        WorkBudgetLedger.id == world.ledger_id,
                        WorkBudgetLedger.root_work_order_id == world.work_order_id,
                        WorkBudgetLedger.owner_key == world.owner_key,
                    )
                )
                counts["budget_ledgers"] = result.rowcount
            if world.work_order_id is not None:
                result = await db.execute(
                    delete(WorkOrder).where(
                        WorkOrder.id == world.work_order_id,
                        WorkOrder.owner_key == world.owner_key,
                    )
                )
                counts["work_orders"] = result.rowcount
            message_ids = [
                item
                for item in (world.user_message_id, world.result_message_id)
                if item is not None
            ]
            if message_ids and world.session_id is not None:
                result = await db.execute(
                    delete(ChatMessage).where(
                        ChatMessage.id.in_(message_ids),
                        ChatMessage.session_id == world.session_id,
                        ChatMessage.session_id.in_(
                            select(ChatSession.id).where(
                                ChatSession.id == world.session_id,
                                ChatSession.user_key == world.owner_key,
                            )
                        ),
                    )
                )
                counts["chat_messages"] = result.rowcount
            if world.session_id is not None:
                result = await db.execute(
                    delete(ChatSession).where(
                        ChatSession.id == world.session_id,
                        ChatSession.user_key == world.owner_key,
                    )
                )
                counts["chat_sessions"] = result.rowcount
            fixture_ids = list(world.fixture_ids.values())
            if fixture_ids:
                result = await db.execute(
                    delete(Document).where(
                        Document.id.in_(fixture_ids),
                        Document.owner_sub == world.owner_key,
                    )
                )
                counts["documents"] = result.rowcount
            await db.commit()
        return counts
