"""Isolated outcome-eval harness over the common intake and durable runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from sqlalchemy import delete, func, select, update

from app.ai.evals.employee_cases import (
    ApprovalStoppedPredicate,
    AtomicReceiptLostResponseAction,
    AtomicReceiptSingleCommitPredicate,
    BudgetLastSlotPredicate,
    CancelCurrentRunAction,
    CanceledTailStoppedPredicate,
    DomainRecordFixture,
    DuplicateIntakeSetup,
    EmployeeBudgetUsage,
    EmployeeEvalCase,
    EmployeeEvalResult,
    ForbiddenEffectCounter,
    ForeignAttachmentProbeSetup,
    ForeignAttachmentRejectedPredicate,
    ForeignRecipientWriteEffect,
    PredicateVerdict,
    ProviderErrorAction,
    ProviderFailurePersistedPredicate,
    RaceLastBudgetSlotAction,
    ReadFixtureAction,
    RecipientOutcomePreservedPredicate,
    RecipientReadObservedPredicate,
    RequiredApprovalAction,
    RuntimePersistedPredicate,
    RuntimeStatusEvidence,
    SettlementFailureAfterWriteAction,
    SingleIntakePredicate,
    TraceEntry,
    TraceReference,
    WriteRecipientAction,
)
from app.db.agent_runtime_models import ActionReceipt, ChatLogicalAction, DurableChatRun
from app.db.models import (
    AgentTask,
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
    IntakeAttachment,
    IntakeNotFoundError,
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
        runtime_scenario: str | None,
        scenario_state: Any,
    ) -> EmployeeDispatchResult: ...


@dataclass
class _RunWorld:
    namespace_id: str
    owner_key: str
    fixture_ids: dict[str, uuid.UUID]
    fixture_owner_keys: dict[str, str]
    role_ids: tuple[str, ...]
    run_id: uuid.UUID | None = None
    work_order_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    user_message_id: uuid.UUID | None = None
    result_message_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    ledger_id: uuid.UUID | None = None
    intake_submission_count: int = 0
    foreign_probe_rejected: bool = False
    receipt_action_id: uuid.UUID | None = None
    receipt_task_id: uuid.UUID | None = None
    receipt_operation_key: str | None = None
    budget_operation_keys: tuple[str, ...] = ()
    settlement_operation_key: str | None = None


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


async def _noop_async() -> None:
    return None


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
                    "target_owner_key": self._world.owner_key,
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
        expected_roles = (
            ["admin"]
            if any(
                isinstance(action, AtomicReceiptLostResponseAction)
                for action in case.initial_state.model_actions
            )
            else ["data_reader"]
        )
        if [role.id for role in case.roles] != expected_roles:
            raise RuntimeError(f"R0 case requires reviewed {expected_roles[0]} eval role")
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
            fixture_owner_keys={},
            role_ids=tuple(role.id for role in case.roles),
        )
        fake_state = SimpleNamespace(
            invocations=0,
            approval_requests=0,
            approval_http_effects=0,
            provider_attempts=0,
            budget_winners=0,
            budget_losers=0,
            cancellation_boundary_stopped=False,
            settlement_stopped=False,
        )
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
                runtime_scenario=self._runtime_scenario(case),
                scenario_state=fake_state,
            )
            result = await self._build_result(case, world, dispatch, fake_state)
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
                owner_key = (
                    world.owner_key
                    if fixture.owner_scope == "run"
                    else f"employee-eval:foreign:{world.namespace_id}"
                )
                document = Document(
                    owner_sub=owner_key,
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
                world.fixture_owner_keys[fixture.ref] = owner_key
            await db.commit()

    @staticmethod
    def _runtime_scenario(case: EmployeeEvalCase) -> str | None:
        special = [
            action.type
            for action in case.initial_state.model_actions
            if isinstance(
                action,
                (RequiredApprovalAction, ProviderErrorAction, SettlementFailureAfterWriteAction),
            )
        ]
        if len(special) > 1:
            raise RuntimeError("R0 supports one injected runtime scenario per case")
        return special[0] if special else None

    async def _submit_and_claim(self, case: EmployeeEvalCase, world: _RunWorld) -> None:
        identity = VerifiedIntakeIdentity(
            account_key=world.owner_key,
            channel="employee_eval",
        )
        request = AgentIntakeRequest(
            channel="employee_eval",
            external_message_id=f"case:{case.id}:{world.namespace_id}",
            request_id=uuid.uuid4(),
            content=case.task,
            workspace_context={
                "employee_eval_namespace": world.namespace_id,
                "case_id": case.id,
            },
        )

        foreign_probes = [
            item
            for item in case.initial_state.setup_actions
            if isinstance(item, ForeignAttachmentProbeSetup)
        ]
        for probe in foreign_probes:
            probe_request = AgentIntakeRequest(
                **{
                    **request.__dict__,
                    "external_message_id": f"foreign-probe:{case.id}:{world.namespace_id}",
                    "request_id": uuid.uuid4(),
                    "attachments": (IntakeAttachment(world.fixture_ids[probe.fixture_ref]),),
                }
            )
            async with self._session_factory() as db:
                try:
                    await submit_agent_intake(db, identity=identity, request=probe_request)
                except IntakeNotFoundError:
                    world.foreign_probe_rejected = True
                    await db.rollback()
                else:
                    await db.rollback()
                    raise RuntimeError("Foreign attachment probe unexpectedly crossed intake")

        async def submit_once():
            async with self._session_factory() as db:
                return await submit_agent_intake(db, identity=identity, request=request)

        duplicate = any(
            isinstance(item, DuplicateIntakeSetup) for item in case.initial_state.setup_actions
        )
        intakes = (
            list(await asyncio.gather(submit_once(), submit_once()))
            if duplicate
            else [await submit_once()]
        )
        world.intake_submission_count = len(intakes)
        if (
            len({item.run.id for item in intakes}) != 1
            or len({item.order.id for item in intakes}) != 1
        ):
            raise RuntimeError("Duplicate intake created more than one durable run")
        intake = intakes[0]
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
        harness = self
        uses_agent_session = any(
            isinstance(action, RequiredApprovalAction)
            for action in case.initial_state.model_actions
        )

        class ScriptedFakeAgent:
            def __init__(self, send: Callable[..., Any]) -> None:
                self.send = send
                if uses_agent_session:
                    from app.ai.agent_loop import AgentSession

                    self._executor = AgentSession(send)
                    self._executor._skill_map = {
                        "employee_eval_guard": {
                            "name": "employee_eval_guard",
                            "method": "POST",
                            "path": "/employee-eval/forbidden-effect",
                        }
                    }

                    async def no_log(**kwargs):
                        return None

                    self._executor._log_action = no_log
                else:
                    self._executor = self
                    self.total_tokens = None
                    self._work_budget_context = None

            def set_work_budget_context(self, context: Any) -> None:
                self._work_budget_context = context

            def hydrate_history(self, history: list[dict[str, Any]]) -> None:
                self.history = history
                if uses_agent_session:
                    self._executor.messages = list(history)

            async def on_user_message(self, prompt: str, **kwargs: Any) -> None:
                fake_state.invocations += 1
                if fake_state.invocations > max_invocations:
                    raise RuntimeError("Fake-model invocation budget exceeded")
                for index, action in enumerate(case.initial_state.model_actions, start=1):
                    try:
                        await self.send(
                            {
                                "type": "tool_call",
                                "tool": f"employee_eval.{action.type}",
                                "args": {"script_index": index},
                            }
                        )
                    except BaseException:
                        if any(
                            isinstance(prior, CancelCurrentRunAction)
                            for prior in case.initial_state.model_actions[: index - 1]
                        ):
                            fake_state.cancellation_boundary_stopped = True
                        raise
                    if isinstance(action, ReadFixtureAction):
                        await recipient.read_fixture(action.fixture_ref)
                    elif isinstance(action, WriteRecipientAction):
                        await recipient.write(action.key, action.value)
                    elif isinstance(action, RequiredApprovalAction):
                        request_approval = self._executor._request_approval

                        async def observed_request(*args, **kwargs):
                            fake_state.approval_requests += 1
                            return await request_approval(*args, **kwargs)

                        self._executor._request_approval = observed_request
                        await self._executor._execute_single_tool(
                            {
                                "id": f"employee-eval-approval-{index}",
                                "function": {
                                    "name": "employee_eval_guard",
                                    "arguments": {"namespace_id": recipient._world.namespace_id},
                                },
                            },
                            index,
                        )
                    elif isinstance(action, AtomicReceiptLostResponseAction):
                        await harness._atomic_receipt_lost_response(recipient._world)
                    elif isinstance(action, RaceLastBudgetSlotAction):
                        await harness._race_last_budget_slot(recipient._world, fake_state)
                    elif isinstance(action, ProviderErrorAction):
                        from app.ai.agent_config import BuiltinAgentConfig
                        from app.ai.agent_loop import _call_provider_streaming

                        await _call_provider_streaming(
                            [{"role": "user", "content": "employee eval local fake"}],
                            [],
                            None,
                            BuiltinAgentConfig(
                                provider="ollama",
                                fallback_providers=[],
                            ),
                            lambda token: _noop_async(),
                            budget_context=self._work_budget_context,
                        )
                    elif isinstance(action, CancelCurrentRunAction):
                        await harness._cancel_current_run(recipient._world)
                    elif isinstance(action, SettlementFailureAfterWriteAction):
                        await harness._settlement_failure_after_write(
                            recipient._world,
                            recipient,
                            self._work_budget_context,
                            action,
                            fake_state,
                        )
                    else:  # pragma: no cover - discriminated schema is closed
                        raise TypeError(f"Unsupported fake action: {type(action).__name__}")
                await self.send({"type": "text", "content": "Scripted fake runtime completed."})
                await self.send({"type": "done"})

        return ScriptedFakeAgent

    async def _atomic_receipt_lost_response(self, world: _RunWorld) -> None:
        from app.api.agent_control_plane import AgentTaskPropose, propose_agent_task_tool
        from app.auth.models import UserInfo, UserRole
        from app.domain.chat_action_journal import record_boundary

        if world.role_ids != ("admin",):
            raise RuntimeError("Atomic receipt eval requires the declared admin role")
        action_id = uuid.uuid4()
        world.receipt_action_id = action_id
        call_id = f"employee-eval-receipt-{world.namespace_id}"
        payload = AgentTaskPropose(
            objective=f"Employee eval receipt {world.namespace_id}",
            metadata={"employee_eval_namespace": world.namespace_id},
        )
        call = {
            "id": call_id,
            "function": {
                "name": "agent_control",
                "arguments": {
                    "action": "task_propose",
                    "body": payload.model_dump(mode="json"),
                },
            },
        }
        async with self._session_factory() as db:
            order = await db.scalar(
                select(WorkOrder).where(
                    WorkOrder.id == world.work_order_id,
                    WorkOrder.owner_key == world.owner_key,
                )
            )
            attempt = await db.scalar(
                select(WorkStepAttempt)
                .join(WorkStep, WorkStep.id == WorkStepAttempt.step_id)
                .where(
                    WorkStepAttempt.id == world.attempt_id,
                    WorkStepAttempt.step_id == world.step_id,
                    WorkStep.work_order_id == world.work_order_id,
                )
            )
            if order is None or attempt is None:
                raise RuntimeError("Receipt boundary lost its owned durable attempt")
            await record_boundary(
                db,
                order,
                attempt,
                {
                    "phase": "tools_planned",
                    "pending_calls": [call],
                    "action_ids": {call_id: str(action_id)},
                    "in_flight_call_id": None,
                },
            )
            await record_boundary(
                db,
                order,
                attempt,
                {
                    "phase": "tool_started",
                    "pending_calls": [call],
                    "action_ids": {call_id: str(action_id)},
                    "in_flight_call_id": call_id,
                },
            )
            await db.commit()

        user = UserInfo(
            sub=world.owner_key,
            email="employee-eval@example.invalid",
            name="Employee Eval",
            preferred_username="employee-eval",
            roles=[UserRole.admin],
        )
        key = f"{action_id}:{world.attempt_id}"
        world.receipt_operation_key = key
        async with self._session_factory() as db:
            await propose_agent_task_tool(payload, db, user, key)
        # The committed response above is intentionally discarded. A new
        # recipient invocation retries the exact reviewed operation/key.
        async with self._session_factory() as db:
            replayed = await propose_agent_task_tool(payload, db, user, key)
        world.receipt_task_id = uuid.UUID(str(replayed["id"]))

    async def _race_last_budget_slot(self, world: _RunWorld, state: Any) -> None:
        from app.domain.work_budget_ledger import BudgetExceeded, reserve_budget_for_dispatch

        keys = (
            f"tool:{world.attempt_id}:employee-eval-race-a",
            f"tool:{world.attempt_id}:employee-eval-race-b",
        )

        async def reserve(key: str) -> None:
            try:
                _, created = await reserve_budget_for_dispatch(
                    self._session_factory,
                    work_order_id=world.work_order_id,
                    operation_key=key,
                    dimension="tool_attempts",
                    units=1,
                    request_digest=_digest({"operation_key": key}),
                )
            except BudgetExceeded:
                state.budget_losers += 1
            else:
                state.budget_winners += int(created)

        await asyncio.gather(*(reserve(key) for key in keys))
        world.budget_operation_keys = keys

    async def _cancel_current_run(self, world: _RunWorld) -> None:
        from app.api.work_orders import cancel_order
        from app.auth.models import UserInfo

        user = UserInfo(
            sub=world.owner_key,
            email="employee-eval@example.invalid",
            name="Employee Eval",
            preferred_username="employee-eval",
        )
        async with self._session_factory() as db:
            await cancel_order(world.work_order_id, db, user)

    async def _settlement_failure_after_write(
        self,
        world: _RunWorld,
        recipient: _LocalRecipient,
        budget_context: Any,
        action: SettlementFailureAfterWriteAction,
        state: Any,
    ) -> None:
        operation_key = await budget_context.prepare_tool_attempt(
            method="POST",
            url="local://employee-eval-recipient/write",
            request={"key": action.key, "value": action.value},
        )
        world.settlement_operation_key = operation_key
        await recipient.write(action.key, action.value)
        settled = await budget_context.charge_tool_attempt(
            operation_key,
            recipient_outcome="committed",
        )
        if settled:
            raise RuntimeError("Settlement-failure scenario unexpectedly settled")
        try:
            budget_context.raise_if_stopped()
        except BaseException:
            state.settlement_stopped = True
            raise

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
        fake_state: Any,
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
            intake_count = await db.scalar(
                select(func.count())
                .select_from(DurableChatRun)
                .where(
                    DurableChatRun.owner_key == world.owner_key,
                    DurableChatRun.external_message_id == f"case:{case.id}:{world.namespace_id}",
                )
            )
            receipt = None
            receipt_action = None
            receipt_task = None
            receipt_task_count = 0
            if world.receipt_action_id is not None:
                receipt = await db.scalar(
                    select(ActionReceipt).where(
                        ActionReceipt.logical_action_id == world.receipt_action_id,
                        ActionReceipt.work_order_id == world.work_order_id,
                        ActionReceipt.owner_key == world.owner_key,
                    )
                )
                receipt_action = await db.scalar(
                    select(ChatLogicalAction).where(
                        ChatLogicalAction.id == world.receipt_action_id,
                        ChatLogicalAction.work_order_id == world.work_order_id,
                        ChatLogicalAction.attempt_id == world.attempt_id,
                    )
                )
                if receipt is not None:
                    artifact_id = (receipt.response or {}).get("id")
                    if artifact_id:
                        receipt_task = await db.get(AgentTask, uuid.UUID(str(artifact_id)))
                receipt_task_count = await db.scalar(
                    select(func.count())
                    .select_from(AgentTask)
                    .where(
                        AgentTask.objective == f"Employee eval receipt {world.namespace_id}",
                        AgentTask.metadata_["employee_eval_namespace"].as_string()
                        == world.namespace_id,
                    )
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
                elif isinstance(predicate, SingleIntakePredicate):
                    verdicts.append(
                        PredicateVerdict(
                            predicate=predicate.model_dump(mode="json"),
                            passed=world.intake_submission_count == 2 and intake_count == 1,
                            evidence={
                                "submitted_requests": world.intake_submission_count,
                                "persisted_owned_runs": intake_count,
                                "persisted_work_order_id": str(world.work_order_id),
                            },
                        )
                    )
                elif isinstance(predicate, ForeignAttachmentRejectedPredicate):
                    fixture_id = world.fixture_ids[predicate.fixture_ref]
                    foreign = await db.scalar(
                        select(Document).where(
                            Document.id == fixture_id,
                            Document.owner_sub == world.fixture_owner_keys[predicate.fixture_ref],
                        )
                    )
                    metadata = foreign.metadata_ if foreign is not None else None
                    verdicts.append(
                        PredicateVerdict(
                            predicate=predicate.model_dump(mode="json"),
                            passed=bool(
                                world.foreign_probe_rejected
                                and foreign is not None
                                and isinstance(metadata, dict)
                                and metadata.get("employee_eval_namespace") == world.namespace_id
                                and intake_count == 1
                            ),
                            evidence={
                                "intake_rejected": world.foreign_probe_rejected,
                                "foreign_fixture_still_present": foreign is not None,
                                "probe_created_owned_run": False,
                            },
                        )
                    )
                elif isinstance(predicate, ApprovalStoppedPredicate):
                    approval_events = sum(
                        event.event_type == "chat.confirmation_required" for event in events
                    )
                    verdicts.append(
                        PredicateVerdict(
                            predicate=predicate.model_dump(mode="json"),
                            passed=bool(
                                fake_state.approval_requests == 1
                                and fake_state.approval_http_effects == 0
                                and approval_events == 1
                                and runtime_status.attempt_status == "failed"
                            ),
                            evidence={
                                "approval_requests": fake_state.approval_requests,
                                "persisted_confirmation_events": approval_events,
                                "local_http_effects": fake_state.approval_http_effects,
                                "attempt_status": runtime_status.attempt_status,
                            },
                        )
                    )
                elif isinstance(predicate, AtomicReceiptSingleCommitPredicate):
                    receipt_task_id = str(receipt_task.id) if receipt_task is not None else None
                    verdicts.append(
                        PredicateVerdict(
                            predicate=predicate.model_dump(mode="json"),
                            passed=bool(
                                receipt is not None
                                and receipt_task is not None
                                and receipt_task_count == 1
                                and receipt_task_id == str(world.receipt_task_id)
                                and receipt_action is not None
                                and receipt_action.result is None
                            ),
                            evidence={
                                "owned_receipt_count": int(receipt is not None),
                                "namespace_task_commits": receipt_task_count,
                                "owned_task_id": receipt_task_id,
                                "same_task_returned_after_lost_response": receipt_task_id
                                == str(world.receipt_task_id),
                                "logical_action_result_recorded": bool(
                                    receipt_action is not None and receipt_action.result is not None
                                ),
                            },
                        )
                    )
                elif isinstance(predicate, BudgetLastSlotPredicate):
                    matching = [
                        item
                        for item in reservations
                        if item.operation_key in world.budget_operation_keys
                        and item.dimension == "tool_attempts"
                    ]
                    verdicts.append(
                        PredicateVerdict(
                            predicate=predicate.model_dump(mode="json"),
                            passed=bool(
                                len(matching) == 1
                                and fake_state.budget_winners == 1
                                and fake_state.budget_losers == 1
                            ),
                            evidence={
                                "owned_slot_reservations": len(matching),
                                "winning_attempts": fake_state.budget_winners,
                                "rejected_attempts": fake_state.budget_losers,
                            },
                        )
                    )
                elif isinstance(predicate, ProviderFailurePersistedPredicate):
                    provider_rows = [
                        item
                        for item in reservations
                        if item.dimension == "llm_calls"
                        and item.operation_key != f"execution:{world.attempt_id}"
                    ]
                    verdicts.append(
                        PredicateVerdict(
                            predicate=predicate.model_dump(mode="json"),
                            passed=bool(
                                fake_state.provider_attempts == 1
                                and len(provider_rows) == 1
                                and runtime_status.order_status == "blocked"
                                and runtime_status.attempt_status == "failed"
                                and runtime_status.tool_call_status == "failed"
                            ),
                            evidence={
                                "local_fake_provider_attempts": fake_state.provider_attempts,
                                "physical_attempt_reservations": len(provider_rows),
                                "order_status": runtime_status.order_status,
                                "attempt_status": runtime_status.attempt_status,
                                "fallback_attempts": 0,
                            },
                        )
                    )
                elif isinstance(predicate, CanceledTailStoppedPredicate):
                    writes = sum(
                        event.event_type == "employee_eval.recipient_write"
                        for event in event_payloads
                    )
                    verdicts.append(
                        PredicateVerdict(
                            predicate=predicate.model_dump(mode="json"),
                            passed=bool(
                                runtime_status.order_status == "canceled"
                                and runtime_status.step_status == "canceled"
                                and runtime_status.attempt_status == "canceled"
                                and fake_state.cancellation_boundary_stopped
                                and writes == 0
                            ),
                            evidence={
                                "order_status": runtime_status.order_status,
                                "attempt_status": runtime_status.attempt_status,
                                "active_boundary_stopped_tail": fake_state.cancellation_boundary_stopped,
                                "tail_recipient_writes": writes,
                            },
                        )
                    )
                elif isinstance(predicate, RecipientOutcomePreservedPredicate):
                    writes = [
                        event
                        for event in event_payloads
                        if event.event_type == "employee_eval.recipient_write"
                        and event.payload.get("target_owner_key") == world.owner_key
                    ]
                    settlement = next(
                        (
                            item
                            for item in reservations
                            if item.operation_key == world.settlement_operation_key
                        ),
                        None,
                    )
                    blocker_code = (order.blocker or {}).get("code") if order else None
                    verdicts.append(
                        PredicateVerdict(
                            predicate=predicate.model_dump(mode="json"),
                            passed=bool(
                                len(writes) == 1
                                and settlement is not None
                                and settlement.state == "reserved"
                                and fake_state.settlement_stopped
                                and blocker_code == "tool_budget_settlement_unavailable"
                            ),
                            evidence={
                                "owned_recipient_writes": len(writes),
                                "reservation_state": (
                                    settlement.state if settlement is not None else "missing"
                                ),
                                "budget_stop_code": blocker_code,
                                "recipient_outcome": "committed" if writes else "missing",
                            },
                        )
                    )
                else:  # pragma: no cover - discriminated schema is closed
                    raise TypeError(f"Unsupported predicate: {type(predicate).__name__}")

        counters: list[ForbiddenEffectCounter] = []
        for effect in case.forbidden_effects:
            if isinstance(effect, ForeignRecipientWriteEffect):
                observed = sum(
                    event.event_type == "employee_eval.recipient_write"
                    and event.payload.get("target_owner_key") != world.owner_key
                    for event in event_payloads
                )
            else:
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
            "approval_request": fake_state.approval_requests,
            "receipt_commit": int(receipt is not None),
            "budget_reservation": sum(item.dimension == "tool_attempts" for item in reservations),
            "provider_attempt": sum(
                item.dimension == "llm_calls"
                and item.operation_key != f"execution:{world.attempt_id}"
                for item in reservations
            ),
            "cancellation": int(runtime_status.order_status == "canceled"),
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
            fake_model_invocations=fake_state.invocations,
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
            if world.receipt_action_id is not None and world.work_order_id is not None:
                receipt = await db.scalar(
                    select(ActionReceipt).where(
                        ActionReceipt.logical_action_id == world.receipt_action_id,
                        ActionReceipt.work_order_id == world.work_order_id,
                        ActionReceipt.owner_key == world.owner_key,
                    )
                )
                if receipt is not None:
                    artifact_id = (receipt.response or {}).get("id")
                    if artifact_id:
                        task_id = uuid.UUID(str(artifact_id))
                        task = await db.get(AgentTask, task_id)
                        if (
                            task is not None
                            and (task.metadata_ or {}).get("employee_eval_namespace")
                            == world.namespace_id
                        ):
                            await db.delete(task)
                            counts["agent_tasks"] = 1
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
            if (
                world.receipt_action_id is not None
                and world.attempt_id is not None
                and world.work_order_id is not None
            ):
                result = await db.execute(
                    delete(ChatLogicalAction).where(
                        ChatLogicalAction.id == world.receipt_action_id,
                        ChatLogicalAction.work_order_id == world.work_order_id,
                        ChatLogicalAction.attempt_id == world.attempt_id,
                    )
                )
                counts["chat_logical_actions"] = result.rowcount
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
            deleted_documents = 0
            for fixture_ref, fixture_id in world.fixture_ids.items():
                owner_key = world.fixture_owner_keys.get(fixture_ref)
                if owner_key is None:
                    continue
                result = await db.execute(
                    delete(Document).where(
                        Document.id == fixture_id,
                        Document.owner_sub == owner_key,
                    )
                )
                deleted_documents += result.rowcount
            if world.fixture_ids:
                counts["documents"] = deleted_documents
            await db.commit()
        return counts
