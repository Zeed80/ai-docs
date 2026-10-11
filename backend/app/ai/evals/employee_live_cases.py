"""E49: strict schema for live employee cases and their results.

A live case runs the real intake, durable runtime, model and capabilities
inside the isolated eval world (``infra/eval-world``). Unlike the scripted R0
cases (``employee_cases``) nothing tells the model what to do: the case gives
the owner, the world it starts from, the task in words, and predicates over
what the database holds afterwards. The answer text is kept for review but is
never what decides a verdict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_REF = r"^[a-z0-9][a-z0-9_-]*$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LiveOwner(_Strict):
    key: str = Field(min_length=1, max_length=60, pattern=_REF)
    role: Literal[
        "admin",
        "manager",
        "accountant",
        "buyer",
        "engineer",
        "technologist",
        "normcontroller",
        "calculator",
        "viewer",
    ]


# ── fixtures: rows the world starts with ─────────────────────────────────────


class SupplierFixture(_Strict):
    type: Literal["supplier"]
    ref: str = Field(min_length=1, max_length=60, pattern=_REF)
    name: str = Field(min_length=1, max_length=300)
    inn: str = Field(pattern=r"^\d{10}(\d{2})?$")
    contact_email: str | None = None
    contact_phone: str | None = None


class InvoiceFixture(_Strict):
    type: Literal["invoice"]
    ref: str = Field(min_length=1, max_length=60, pattern=_REF)
    supplier_ref: str = Field(min_length=1, max_length=60, pattern=_REF)
    invoice_number: str = Field(min_length=1, max_length=60)
    total_amount: float = Field(ge=0)
    status: Literal["draft", "needs_review", "approved", "rejected", "paid"] = "needs_review"
    owner_scope: Literal["run", "foreign"] = "run"
    due_in_days: float | None = Field(default=None, ge=-365, le=365)
    # As extraction leaves them: text from the document, possibly hostile.
    special_marks: str | None = Field(default=None, max_length=2000)


class DocumentFixture(_Strict):
    """A processed document: a real PDF in the world's store, its text in
    chunks and extraction, as the ingest pipeline leaves it."""

    type: Literal["document"]
    ref: str = Field(min_length=1, max_length=60, pattern=_REF)
    file_name: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20000)
    doc_type: Literal[
        "invoice", "letter", "contract", "drawing", "commercial_offer", "act", "waybill", "other"
    ] = "other"
    owner_scope: Literal["run", "foreign"] = "run"


class RowFixture(_Strict):
    """A row of a whitelisted table (see ``live_runner.TABLES``).

    ``links`` fills foreign-key columns with the id of another fixture, so a
    payment schedule can point at a fixture invoice without a fixed UUID.
    """

    type: Literal["row"]
    ref: str = Field(min_length=1, max_length=60, pattern=_REF)
    table: str = Field(pattern=r"^[a-z_]+$")
    values: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, str] = Field(default_factory=dict)


LiveFixture = Annotated[
    SupplierFixture | InvoiceFixture | DocumentFixture | RowFixture,
    Field(discriminator="type"),
]


# ── acceptance predicates: facts in the database afterwards ─────────────────


class FieldEquals(_Strict):
    """A fixture row's column holds exactly this value."""

    type: Literal["field_equals"]
    fixture_ref: str = Field(pattern=_REF)
    field: str = Field(pattern=r"^[a-z_]+$")
    value: Any


class FieldContains(_Strict):
    """A fixture row's text column contains ``value`` after normalization.

    ``digits`` compares only digits (phones, accounts), ``casefold`` ignores
    case and treats ё as е; the wording around the value is the model's.
    """

    type: Literal["field_contains"]
    fixture_ref: str = Field(pattern=_REF)
    # Several fields: contains = any of them, unchanged/changed = all of them.
    field: str | tuple[str, ...] = Field(min_length=1)
    value: str = Field(min_length=1, max_length=500)
    normalize: Literal["casefold", "digits"] = "casefold"


class FieldUnchanged(_Strict):
    type: Literal["field_unchanged"]
    fixture_ref: str = Field(pattern=_REF)
    # Several fields: contains = any of them, unchanged/changed = all of them.
    field: str | tuple[str, ...] = Field(min_length=1)


class OrderStatusIn(_Strict):
    """The durable order ended in one of these states (e.g. waiting_approval)."""

    type: Literal["order_status_in"]
    statuses: tuple[str, ...] = Field(min_length=1, max_length=10)


class ActionAttempted(_Strict):
    """The journal holds a call of this capability action (any outcome)."""

    type: Literal["action_attempted"]
    capability: str = Field(pattern=r"^[a-z_]+$")
    action: str = Field(pattern=r"^[a-z_]+$")


class StoppedForConfirmation(_Strict):
    """The run stopped before this gated action, waiting for the owner.

    A work-order step shows it as ``waiting_approval``; a durable chat turn
    stops with the action ``waiting_confirmation`` in its journal and the
    owner's card (E10). Either way the action did not run.
    """

    type: Literal["stopped_for_confirmation"]
    capability: str = Field(pattern=r"^[a-z_]+$")
    action: str = Field(pattern=r"^[a-z_]+$")


class RowsCreated(_Strict):
    """Rows of a whitelisted table created during the run that match ``where``.

    ``where`` maps a column (or ``column.key`` inside a JSON column) to a
    matcher: a plain value (equal), ``{"contains": text}`` (case-insensitive,
    on the JSON text for lists and objects), ``{"ref": fixture}`` (that
    fixture's id) or ``{"in": [values]}``.
    """

    type: Literal["rows_created"]
    table: str = Field(pattern=r"^[a-z_]+$")
    where: dict[str, Any] = Field(default_factory=dict)
    min: int = Field(default=1, ge=0, le=1000)
    max: int | None = Field(default=None, ge=0, le=1000)


LivePredicate = Annotated[
    FieldEquals
    | FieldContains
    | FieldUnchanged
    | OrderStatusIn
    | ActionAttempted
    | StoppedForConfirmation
    | RowsCreated,
    Field(discriminator="type"),
]


# ── forbidden effects: counted, never excused by the answer text ────────────


class FieldChanged(_Strict):
    type: Literal["field_changed"]
    fixture_ref: str = Field(pattern=_REF)
    # Several fields: contains = any of them, unchanged/changed = all of them.
    field: str | tuple[str, ...] = Field(min_length=1)


class ForeignFixtureChanged(_Strict):
    """Any column of a fixture owned by someone else changed."""

    type: Literal["foreign_fixture_changed"]


class ActionSucceeded(_Strict):
    """A call of this action committed (e.g. a gated action ran without approval)."""

    type: Literal["action_succeeded"]
    capability: str = Field(pattern=r"^[a-z_]+$")
    action: str = Field(pattern=r"^[a-z_]+$")


class RowsCreatedForbidden(_Strict):
    """No more than ``max`` such rows may appear (e.g. a second draft)."""

    type: Literal["rows_created_over"]
    table: str = Field(pattern=r"^[a-z_]+$")
    where: dict[str, Any] = Field(default_factory=dict)
    max: int = Field(default=0, ge=0, le=1000)


class ActionCalled(_Strict):
    """The action was called at all — run, refused or stopped for confirmation.

    For a case where asking the owner to confirm it is already wrong (an RFQ
    to a supplier that does not exist).
    """

    type: Literal["action_called"]
    capability: str = Field(pattern=r"^[a-z_]+$")
    action: str = Field(pattern=r"^[a-z_]+$")


class AnswerLeaks(_Strict):
    """A canary — a secret or another user's text — reaches the person: in
    the reply or on their desktop (a published table counts as much).

    Only for security cases: there what is shown *is* the harm (a leaked key,
    another person's reminder). Acceptance never reads text.
    """

    type: Literal["answer_leaks"]
    canary: str = Field(min_length=6, max_length=200)


class OwnerRoleChanged(_Strict):
    """The run's owner ended with a different role than they started with."""

    type: Literal["owner_role_changed"]


class MayChange(_Strict):
    fixture_ref: str = Field(pattern=_REF)
    field: str = Field(pattern=r"^[a-z_]+$")


LiveForbidden = Annotated[
    FieldChanged
    | ForeignFixtureChanged
    | ActionSucceeded
    | RowsCreatedForbidden
    | ActionCalled
    | AnswerLeaks
    | OwnerRoleChanged,
    Field(discriminator="type"),
]


class BrowserGrant(_Strict):
    """A computer-use grant the owner gives this run's work order (made after
    intake, since a grant belongs to an order). Hosts are the world's sites."""

    actions: tuple[str, ...] = Field(min_length=1, max_length=20)
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=10)
    max_actions: int = Field(default=20, ge=1, le=200)


class LiveBudget(_Strict):
    max_wall_seconds: int = Field(ge=30, le=3600)
    max_llm_calls: int = Field(ge=1, le=200)
    max_tool_attempts: int = Field(ge=0, le=200)


class LiveEmployeeCase(_Strict):
    case_version: Literal["employee_live_case_v1"]
    id: str = Field(min_length=1, max_length=120, pattern=_REF)
    # "security" (E51) is scored apart from usefulness and never averaged in.
    group: Literal["browser", "files", "data", "communications", "coordination", "security"]
    complexity: Literal["simple", "multi_step", "recovery"]
    # A negative control is a case the harness must report as failed: it
    # proves a predicate cannot pass by accident. It never counts towards
    # the employee's score.
    control: Literal["none", "expected_fail"] = "none"
    owner: LiveOwner
    fixtures: tuple[LiveFixture, ...] = Field(min_length=1, max_length=50)
    task: str = Field(min_length=1, max_length=12000)
    acceptance_predicates: tuple[LivePredicate, ...] = Field(min_length=1, max_length=20)
    forbidden_effects: tuple[LiveForbidden, ...] = Field(min_length=1, max_length=20)
    budget: LiveBudget
    # Fixture fields the employee may change besides those the acceptance
    # predicates expect to change; any other fixture field that changes fails
    # the case (implicit check).
    may_change: tuple[MayChange, ...] = ()
    # Tables the employee may add rows to besides those the acceptance
    # predicates expect; any other new row fails the case (implicit check).
    # Proposed agent tasks are always allowed: proposing is the initiative
    # the agent is asked to show.
    may_create: tuple[str, ...] = ()
    browser_grant: BrowserGrant | None = None

    @model_validator(mode="after")
    def _references(self) -> LiveEmployeeCase:
        refs = [f.ref for f in self.fixtures]
        if len(refs) != len(set(refs)):
            raise ValueError("fixture refs must be unique")
        suppliers = {f.ref for f in self.fixtures if isinstance(f, SupplierFixture)}
        for fixture in self.fixtures:
            if isinstance(fixture, InvoiceFixture) and fixture.supplier_ref not in suppliers:
                raise ValueError(f"unknown supplier_ref: {fixture.supplier_ref}")
            if isinstance(fixture, RowFixture):
                # Rows are created after suppliers and invoices, in list order.
                earlier = {
                    f.ref
                    for f in self.fixtures[: self.fixtures.index(fixture)]
                    if isinstance(f, RowFixture)
                } | {f.ref for f in self.fixtures if not isinstance(f, RowFixture)}
                unknown_links = sorted(set(fixture.links.values()) - earlier)
                if unknown_links:
                    raise ValueError(
                        f"{fixture.ref}: links must name earlier fixtures: {unknown_links}"
                    )
        used = {
            item.fixture_ref
            for item in (*self.acceptance_predicates, *self.forbidden_effects, *self.may_change)
            if hasattr(item, "fixture_ref")
        }
        unknown = sorted(used - set(refs))
        if unknown:
            raise ValueError(f"unknown fixture refs: {', '.join(unknown)}")
        return self


class LiveCaseCorpus(_Strict):
    schema_version: Literal["employee_live_corpus_v1"]
    fixture_version: str = Field(min_length=1, max_length=100)
    cases: tuple[LiveEmployeeCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> LiveCaseCorpus:
        ids = [c.id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        return self


# ── results ──────────────────────────────────────────────────────────────────


class Verdict(_Strict):
    check: dict[str, Any]
    passed: bool
    evidence: dict[str, Any]


class ModelIdentity(_Strict):
    """What actually answered: recorded from the world, not from the case."""

    agent_provider: str
    agent_model: str
    orchestrator_provider: str | None
    orchestrator_model: str | None
    setup_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class LiveUsage(_Strict):
    llm_calls: int = Field(ge=0)
    tool_attempts: int = Field(ge=0)
    tokens_known: int = Field(ge=0)
    llm_calls_with_unknown_tokens: int = Field(ge=0)
    wall_seconds: float = Field(ge=0)
    over_budget: bool


class ToolTraceEntry(_Strict):
    capability: str | None
    action: str | None
    status: str
    request_digest: str


class LiveEmployeeResult(_Strict):
    result_version: Literal["employee_live_result_v1"] = "employee_live_result_v1"
    case_id: str
    case_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    group: str
    complexity: str
    control: str
    fixture_version: str
    code_revision: str
    runtime_mode: Literal["live"] = "live"
    # A negative control proves the harness, not the employee.
    live_model_metrics_eligible: bool
    model: ModelIdentity
    namespace_id: str
    owner_sub: str
    work_order_id: str | None
    order_status: str | None
    order_blocker: dict[str, Any] | None
    predicate_verdicts: tuple[Verdict, ...]
    forbidden_counters: tuple[Verdict, ...]
    usage: LiveUsage
    tool_trace: tuple[ToolTraceEntry, ...]
    answer_excerpt: str | None
    error: str | None
    status: Literal["passed", "failed", "error"]
    started_at: str
    finished_at: str


def load_live_corpus(path: str | Path) -> LiveCaseCorpus:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("live corpus must be a YAML mapping")
    return LiveCaseCorpus.model_validate(raw)
