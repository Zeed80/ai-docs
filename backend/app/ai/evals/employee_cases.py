"""Strict, versioned schemas for outcome-based employee evaluation cases."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmployeeOwner(_StrictModel):
    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")


class EmployeeRole(_StrictModel):
    id: str = Field(min_length=1, max_length=100)


class EmployeeGrant(_StrictModel):
    capability: str = Field(min_length=1, max_length=100)
    actions: tuple[str, ...] = Field(min_length=1, max_length=20)


class DomainRecordFixture(_StrictModel):
    type: Literal["domain_record"]
    ref: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    value: dict[str, Any]


EmployeeFixture = Annotated[DomainRecordFixture, Field(discriminator="type")]


class ReadFixtureAction(_StrictModel):
    type: Literal["read_fixture"]
    fixture_ref: str = Field(min_length=1, max_length=100)


class WriteRecipientAction(_StrictModel):
    type: Literal["write_recipient"]
    key: str = Field(min_length=1, max_length=100)
    value: dict[str, Any]


FakeModelAction = Annotated[
    ReadFixtureAction | WriteRecipientAction,
    Field(discriminator="type"),
]


class EmployeeInitialState(_StrictModel):
    model_actions: tuple[FakeModelAction, ...] = Field(min_length=1, max_length=20)


class RuntimePersistedPredicate(_StrictModel):
    type: Literal["runtime_persisted"]


class RecipientReadObservedPredicate(_StrictModel):
    type: Literal["recipient_read_observed"]
    fixture_ref: str = Field(min_length=1, max_length=100)


AcceptancePredicate = Annotated[
    RuntimePersistedPredicate | RecipientReadObservedPredicate,
    Field(discriminator="type"),
]


class RecipientWriteForbiddenEffect(_StrictModel):
    type: Literal["recipient_write"]
    max_count: int = Field(default=0, ge=0, le=100)


ForbiddenEffect = Annotated[RecipientWriteForbiddenEffect, Field(discriminator="type")]


class EmployeeBudget(_StrictModel):
    max_active_seconds: int = Field(ge=10, le=7200)
    max_llm_calls: int = Field(ge=0, le=100)
    max_tool_attempts: int = Field(ge=0, le=1000)
    max_replans: int = Field(default=0, ge=0, le=20)
    max_fake_model_invocations: int = Field(default=1, ge=1, le=10)


AllowedEffect = Literal["runtime_persistence", "recipient_read", "assistant_response"]


class EmployeeEvalCase(_StrictModel):
    case_version: Literal["employee_case_v1"]
    id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    group: Literal["browser", "files", "data", "communications", "coordination"]
    initial_state: EmployeeInitialState
    owner: EmployeeOwner
    roles: tuple[EmployeeRole, ...] = Field(min_length=1, max_length=20)
    grants: tuple[EmployeeGrant, ...] = Field(max_length=20)
    task: str = Field(min_length=1, max_length=12000)
    fixtures: tuple[EmployeeFixture, ...] = Field(min_length=1, max_length=50)
    allowed_effects: tuple[AllowedEffect, ...] = Field(min_length=1)
    acceptance_predicates: tuple[AcceptancePredicate, ...] = Field(min_length=1, max_length=20)
    forbidden_effects: tuple[ForbiddenEffect, ...] = Field(min_length=1, max_length=20)
    budget: EmployeeBudget
    runtime_mode: Literal["fake"]

    @model_validator(mode="after")
    def validate_references(self) -> EmployeeEvalCase:
        fixture_refs = [fixture.ref for fixture in self.fixtures]
        if len(fixture_refs) != len(set(fixture_refs)):
            raise ValueError("fixture refs must be unique")
        referenced = {
            item.fixture_ref
            for item in (*self.initial_state.model_actions, *self.acceptance_predicates)
            if isinstance(item, (ReadFixtureAction, RecipientReadObservedPredicate))
        }
        unknown = sorted(referenced - set(fixture_refs))
        if unknown:
            raise ValueError(f"unknown fixture refs: {', '.join(unknown)}")
        if len(self.allowed_effects) != len(set(self.allowed_effects)):
            raise ValueError("allowed_effects must be unique")
        return self


class EmployeeCaseCorpus(_StrictModel):
    schema_version: Literal["employee_case_corpus_v1"]
    fixture_version: str = Field(min_length=1, max_length=100)
    cases: tuple[EmployeeEvalCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_case_ids(self) -> EmployeeCaseCorpus:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("employee case ids must be unique")
        return self


class PredicateVerdict(_StrictModel):
    predicate: dict[str, Any]
    passed: bool
    evidence: dict[str, Any]


class ForbiddenEffectCounter(_StrictModel):
    effect: dict[str, Any]
    observed_count: int = Field(ge=0)
    passed: bool
    evidence: dict[str, Any]


class EmployeeBudgetUsage(_StrictModel):
    configured: EmployeeBudget
    fake_model_invocations: int = Field(ge=0)
    execution_fence_reservations: int = Field(ge=0)
    physical_llm_calls: int = Field(ge=0)
    physical_tool_attempts: int = Field(ge=0)
    reported_tokens: int | None = Field(default=None, ge=0)
    reported_cost_usd: float | None = Field(default=None, ge=0)


class RuntimeStatusEvidence(_StrictModel):
    dispatcher_settled: bool
    order_status: str
    step_status: str
    attempt_status: str
    tool_call_status: str
    verification_dispatched: bool


class TraceEntry(_StrictModel):
    event_id: str
    sequence: int = Field(ge=1)
    event_type: str
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class TraceReference(_StrictModel):
    work_order_id: str
    entries: tuple[TraceEntry, ...]


class EmployeeEvalResult(_StrictModel):
    result_version: Literal["employee_eval_result_v1"] = "employee_eval_result_v1"
    case_id: str
    case_version: str
    fixture_version: str
    code_revision: str
    config_version: str
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fake_model_version: str
    runtime_mode: Literal["fake"]
    live_model_metrics_eligible: Literal[False] = False
    namespace_id: str
    owner_key: str
    run_id: str
    work_order_id: str
    step_id: str
    attempt_id: str
    fixture_ids: dict[str, str]
    predicate_verdicts: tuple[PredicateVerdict, ...]
    forbidden_counters: tuple[ForbiddenEffectCounter, ...]
    budget_usage: EmployeeBudgetUsage
    runtime_status: RuntimeStatusEvidence
    trace_reference: TraceReference
    status: Literal["passed", "failed"]
    cleanup_performed: bool = False
    cleanup_counts: dict[str, int] = Field(default_factory=dict)


def load_employee_case_corpus(path: str | Path) -> EmployeeCaseCorpus:
    """Load with SafeLoader, then reject every unknown field fail-closed."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("employee case corpus must be a YAML mapping")
    return EmployeeCaseCorpus.model_validate(raw)


def live_model_results(results: list[EmployeeEvalResult]) -> list[EmployeeEvalResult]:
    """Return only live results; fake R0 runs are intentionally never eligible."""
    return [result for result in results if result.live_model_metrics_eligible]
