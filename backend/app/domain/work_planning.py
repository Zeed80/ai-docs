"""Capability-grounded planning, dataflow resolution, and bounded replanning."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.capability_manifest import CapabilityDefinition, load_capability_manifest
from app.db.models import WorkOrder, WorkStep
from app.domain.work_orders import (
    WORK_TRANSITIONS,
    append_event,
    create_work_plan,
    is_exploratory,
    transition_work_order,
)

# Ф4-re post-mortem (AGENT_AUTONOMY_ROADMAP.md, pilot 5db58ac6): found by
# reading this exact order's plan.fallback_used events after it went
# `blocked` — from plan revision ~19 through 31 (the last 53 of its 135
# minutes, 11 replans in a row) EVERY generate_capability_plan call failed
# with the identical pydantic error ("steps: Field required", input already
# shaped like {"text": ...} or {"result": {...}}). The reasoning model was
# imitating the shape of the previous fallback agent_turn step's own raw
# narration output, which plan_work_order fed straight back as one of
# completed_steps — nothing distinguished "this was a free-form synthesis
# reply" from "this is what a plan step's output normally looks like", and
# nothing told the model its last JSON was rejected or why. Once one
# fallback narration entered completed_steps the next plan call reliably
# copied its shape, producing another fallback narration — self-reinforcing,
# and it never broke on its own within the remaining budget. _MAX_
# CONSECUTIVE_PLANNER_FALLBACKS is the backstop for if the primary fix
# (planner_error_context below + relabeling agent_turn output in
# plan_work_order's completed_context) doesn't self-correct within a
# handful of tries anyway — stop burning wall-clock/replan budget on a
# repeat of the identical failure instead of riding it out to the budget
# ceiling as this pilot did.
_MAX_CONSECUTIVE_PLANNER_FALLBACKS = 5

# Ф4-re (AGENT_AUTONOMY_ROADMAP.md): found live on the persistence
# re-verification pilot — the path character class didn't allow "[" or "]",
# so a model-written reference using bracket array indexing
# (${steps.X.output.result.items[0].url}, the common JS/JSON-path
# convention) failed to match this regex at all. resolve()'s fallback for a
# non-matching string is to return it UNCHANGED — the literal template
# string was passed straight through as if it were the real value, silently
# (no error, no replan-triggering exception): every capability call fed
# this got e.g. text="${steps.discover.output.result.items[0].text}" as its
# actual argument, which of course produced zero real catalog entries. Path
# character class widened to accept "[" and "]"; _path_get below is what
# actually interprets them.
_REF = re.compile(r"^\$\{steps\.([a-zA-Z0-9_-]+)\.output(?:\.([a-zA-Z0-9_.\[\]-]+))?\}$")
# The same reference EMBEDDED in a longer string ("каталог ${steps.x.output.name}").
# Without this the whole string used to pass through untouched, and a literal
# "${steps.discover_suppliers.output.suppliers[0].name}" was stored as a
# supplier NAME in production (two such rows found on the live database).
_REF_INLINE = re.compile(r"\$\{steps\.([a-zA-Z0-9_-]+)\.output(?:\.([a-zA-Z0-9_.\[\]-]+))?\}")


class PlannedChildSpec(BaseModel):
    """One child WorkOrder a "decompose" step (Б11) spawns."""

    objective: str = Field(min_length=1, max_length=2000)
    description: str | None = None
    # Overrides the equal-split share of the parent's token_budget (Б15) for
    # just this child — see _split_child_budgets in tasks/work_orders.py.
    budgets: dict[str, Any] | None = None


class PlannedStep(BaseModel):
    step_key: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=120)
    title: str = Field(min_length=1, max_length=500)
    kind: str = Field(pattern="^(capability|agent_turn|decompose|synthesize)$")
    capability: str | None = None
    action: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    success_predicate: dict[str, Any] = Field(
        default_factory=lambda: {"type": "no_error_and_nonempty_output"}
    )
    risk_level: str = Field("low", pattern="^(low|medium|high|critical)$")
    max_attempts: int = Field(3, ge=1, le=10)
    timeout_seconds: int = Field(600, ge=1, le=3600)

    @field_validator("success_predicate", mode="before")
    @classmethod
    def _coerce_success_predicate(cls, value: Any) -> Any:
        """Ф4 (AGENT_AUTONOMY_ROADMAP.md): found live on the pilot's first
        real planner call — the reasoning model sometimes describes this in
        prose (e.g. "Supplier created successfully with a new supplier_id")
        instead of the {"type": ...} shape the base system prompt names as a
        schema field but never shows a worked example of. WorkStep.
        success_predicate is stored for audit/display only — nothing in the
        runtime actually evaluates it (order-level completion goes through
        WorkAcceptanceCriterion instead, an entirely separate structure) — so
        rejecting a plan outright over this shape mismatch was pure loss: the
        whole multi-step/decompose plan got thrown away for a single-step
        agent_turn fallback (plan_work_order's exception handler) that never
        used the exploratory machinery. Coercing a bare string into a real
        dict keeps the plan; no downstream code needs a specific "type".

        Also observed live on the Ф4 pilot's replan 7: the same model wrote
        a bare ``true`` for this field on one step instead of a string or a
        dict — same root cause (the base prompt names the field but shows no
        worked example), same "purely cosmetic, nothing evaluates it" fix
        rationale as the string case above.
        """
        if isinstance(value, str):
            return {"type": "custom", "description": value}
        if isinstance(value, bool):
            return {"type": "custom", "description": str(value)}
        return value

    @model_validator(mode="after")
    def executor_is_complete(self) -> PlannedStep:
        if self.kind == "capability" and (not self.capability or not self.action):
            raise ValueError("capability step requires capability and action")
        if self.kind == "decompose":
            children = self.input.get("children")
            if not isinstance(children, list) or not children:
                raise ValueError("decompose step requires a non-empty input.children list")
            for child in children:
                PlannedChildSpec.model_validate(child)  # raises on malformed entry
        return self


class PlannedWork(BaseModel):
    assumptions: list[str] = Field(default_factory=list)
    steps: list[PlannedStep] = Field(min_length=1, max_length=30)
    verification_plan: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class PlannerSnapshot:
    """Immutable server-owned input for one detached planner execution."""

    work_order_id: uuid.UUID
    owner_key: str
    status: str
    plan_revision: int
    objective: str
    description: str | None
    constraints: dict[str, Any]
    budgets: dict[str, Any]
    metadata_: dict[str, Any]
    blocker: dict[str, Any] | None
    completed_context: list[dict[str, Any]]
    authority_digest: str
    connector_hints: list[dict[str, Any]]
    capability_catalog: list[dict[str, Any]]
    digest: str


def _action_names(capability: CapabilityDefinition) -> set[str]:
    action = (capability.parameters.get("properties") or {}).get("action") or {}
    description = str(action.get("description") or "")
    return {
        token.strip()
        for token in re.split(r"\s*[|,]\s*", description)
        if re.fullmatch(r"[a-z][a-z0-9_]*", token.strip())
    }


def validate_capability_plan(
    plan: PlannedWork, *, satisfied_keys: frozenset[str] | set[str] = frozenset()
) -> PlannedWork:
    """Validate a planned DAG against the live manifest.

    ``satisfied_keys`` are steps that already succeeded in earlier revisions:
    a replan must not repeat them, so a dependency on one is already met and
    is dropped. Rejecting it made every replan with a final synthesize step
    invalid, and keeping it would leave the step pending forever (live
    2026-10-06).
    """
    manifest = load_capability_manifest()
    capabilities = manifest.by_name
    keys = {step.step_key for step in plan.steps}
    if len(keys) != len(plan.steps):
        raise ValueError("planner returned duplicate step keys")
    for step in plan.steps:
        step.depends_on = [
            dependency
            for dependency in step.depends_on
            if dependency in keys or dependency not in satisfied_keys
        ]
    for step in plan.steps:
        unknown = set(step.depends_on) - keys
        if unknown or step.step_key in step.depends_on:
            raise ValueError(f"invalid dependencies for {step.step_key}: {sorted(unknown)}")
        if step.kind == "agent_turn":
            # Headless agent_turn execution is retired (E21.2b5): such a step
            # can only fail. Reject the plan so the model sees why and replans.
            raise ValueError(
                f"step {step.step_key}: agent_turn is not executable in durable work; "
                "use capability, decompose or synthesize steps"
            )
        if step.kind != "capability":
            continue
        capability = capabilities.get(str(step.capability))
        if capability is None:
            raise ValueError(f"unknown capability: {step.capability}")
        actions = _action_names(capability)
        if actions and step.action not in actions:
            raise ValueError(f"unknown action {step.capability}.{step.action}")
        if manifest.is_gated(str(step.capability), step.action):
            step.risk_level = "high"
    # Domain validation performs complete cycle detection when persisting.
    return plan


def _planner_catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": capability.name,
            "description": capability.description[:1400],
            "actions": sorted(_action_names(capability)),
            "parameters": capability.parameters,
            "gated_actions": capability.gate_actions,
        }
        for capability in load_capability_manifest().capabilities
    ]


async def generate_capability_plan(
    order: WorkOrder | PlannerSnapshot,
    *,
    completed_context: list[dict[str, Any]] | None = None,
    failure_context: dict[str, Any] | None = None,
    planner_error_context: str | None = None,
    connector_hints: list[dict[str, Any]] | None = None,
    capability_catalog: list[dict[str, Any]] | None = None,
    budget_context: Any | None = None,
) -> PlannedWork:
    """Ask the reasoning model for a bounded DAG grounded in the live manifest."""
    from app.ai.model_resolver import get_reasoning_model
    from app.ai.ollama_client import generate_json

    model_config = get_reasoning_model()
    # Ф5 (AGENT_AUTONOMY_ROADMAP.md): self-learning connector hints — only
    # for exploratory orders (a bounded/grounded order's DAG doesn't involve
    # open-ended web discovery in the first place). Best-effort: an empty
    # list changes nothing about the prompt below, and find_connector_hints
    # itself never raises.
    resolved_connector_hints: list[dict[str, Any]] = connector_hints or []
    if connector_hints is None and is_exploratory(order):
        from app.ai.connectors import find_connector_hints

        resolved_connector_hints = await find_connector_hints(order.objective)
    prompt = json.dumps(
        {
            "objective": order.objective,
            "description": order.description,
            "constraints": order.constraints,
            "budgets": order.budgets,
            "new_instructions": (order.metadata_ or {}).get("instructions", []),
            "completed_steps": completed_context or [],
            "last_failure": failure_context,
            "last_planner_error": planner_error_context,
            "connector_hints": resolved_connector_hints,
            "capabilities": capability_catalog
            if capability_catalog is not None
            else _planner_catalog(),
        },
        ensure_ascii=False,
        default=str,
    )
    system = """You are a durable task planner. Return JSON only.
Build the smallest executable DAG using only listed capability/action pairs. Each external
operation is one capability step. Step kinds are "capability", "decompose" and "synthesize";
there is no free-form agent_turn step. When the objective needs a written answer, end with one
"synthesize" step (input {"instruction": what to write}, depends_on the steps it summarizes):
it writes the final text from completed step results, without tools. Pass data between steps with exact references like
${steps.lookup.output.result.items}. Never repeat completed work during replanning.
Schema: {assumptions:[string], steps:[{step_key,title,kind,capability?,action?,input,
depends_on,success_predicate,risk_level,max_attempts,timeout_seconds}],
verification_plan:{mode,checks}}. Gated actions must be high risk."""
    if planner_error_context:
        # Ф4-re post-mortem: the previous call's own rejection reason was
        # never shown to the model before this fix — it had no way to know
        # its last JSON didn't match the schema, or why, so it kept
        # repeating the same mistake (see _MAX_CONSECUTIVE_PLANNER_FALLBACKS
        # docstring above for the live evidence).
        system += f"""

Your previous plan JSON for this order was REJECTED: {planner_error_context[:300]}
You MUST return a JSON object matching exactly the Schema above — never a bare {{"text": ...}}
or {{"result": ...}} conversational reply, even if the objective feels finished or you only
have a short answer to give. If nothing more needs doing, plan a single "synthesize" step
with the final answer as its instruction — never a top-level free-form object instead of
{{assumptions, steps, verification_plan}}."""
    if is_exploratory(order):
        # Ф1.A (AGENT_AUTONOMY_ROADMAP.md): constraints.mode="exploratory" is
        # already visible to the model inside the prompt JSON above — this
        # tells it what to DO about that, since "smallest executable DAG"
        # above is the wrong instinct here: the full scope of an open-ended
        # search isn't known up front, so don't try to plan it all now.
        #
        # Rewritten (2026-08-20, user feedback after the Ф4 pilot ended
        # "blocked"): the previous wording here ended with "Reporting a
        # genuine gap in not_found is success, not failure" — that told the
        # model conceding a source was just as good as actually finding it,
        # after a *single* attempt. The goal is not an honestly-worded
        # give-up; it is completing the objective, or genuinely exhausting
        # reasonable strategies first. not_found is the last resort after
        # real, varied effort — not a comfortable default.
        system += """

This objective is exploratory (constraints.mode == "exploratory"): its full scope is not
knowable up front (e.g. "find and structure all supplier catalogs" — the set of suppliers
and how to reach each one is discovered, not given). Do not attempt one large DAG covering
everything. Plan only the next small horizon (1-3 steps: discover a batch of sources, or
fetch/extract from ones already found) and rely on replanning with completed_steps/
last_failure to continue once this horizon finishes — the same incremental loop already
used for ordinary bounded replanning. When the objective naturally splits into independent
units (one per supplier, one per source), prefer a single "decompose" step that spawns one
child WorkOrder per unit over a flat list of steps for all of them — children get their own
budget share and run independently (see PlannedChildSpec). connector_hints (if non-empty)
lists domains/patterns that have actually worked before for similar objectives, each with the
strategy (queries/sample URL) that succeeded — prefer trying these first over generic
discovery from scratch when they plausibly match the current objective.

Your goal is to actually complete the objective, not to produce an honest-sounding reason
for not completing it. Before writing anything off as not_found: try a different query, a
different source, a different capability/tool, or a different phrasing — a single failed
attempt is not evidence something cannot be found. When last_failure shows a step failed,
the correct response is usually to retry with an adjusted approach, not to concede that
item. Only report an item as not_found after multiple, genuinely different attempts have
failed — an independent verifier checks that each not_found entry reflects real varied
effort, not a first-attempt bailout, and will reject the report otherwise. The plan's final
step (of the whole objective, once every unit is covered or genuinely exhausted) is a
"synthesize" step and must produce output shaped exactly {"text": <human summary>, "coverage": {"covered": [...],
"partial": [...], "not_found": [{"item":..., "reason":..., "attempts":[...]}, ...]}} — each
not_found entry's "attempts" lists what was actually tried and how each attempt failed."""
    raw = await generate_json(
        prompt,
        model=model_config.model,
        provider=model_config.provider,
        system=system,
        temperature=0.0,
        max_tokens=8192,
        timeout_seconds=180,
        budget_context=budget_context,
    )
    try:
        return validate_capability_plan(
            PlannedWork.model_validate(raw),
            satisfied_keys={
                str(item.get("step_key"))
                for item in completed_context or []
                if isinstance(item, dict) and item.get("step_key")
            },
        )
    except (ValidationError, ValueError) as exc:
        raise ValueError(f"planner produced an invalid capability DAG: {exc}") from exc


def fallback_plan(order: WorkOrder, *, reason: str | None = None) -> PlannedWork:
    prompt = order.objective + (f"\n\n{order.description}" if order.description else "")
    return PlannedWork(
        assumptions=[f"Capability planner fallback: {reason}" if reason else "Planner fallback"],
        steps=[
            PlannedStep(
                step_key="synthesize",
                title="Выполнить поручение автономным исполнителем",
                kind="agent_turn",
                input={"prompt": prompt},
                timeout_seconds=int((order.budgets or {}).get("timeout_seconds", 600)),
            )
        ],
        verification_plan={"mode": "deterministic_then_independent"},
    )


_MAX_STEP_OUTPUT_CHARS = 1500


def _summarize_step_output(step: WorkStep) -> Any:
    """Bound what a succeeded step contributes to the next plan call's
    completed_steps context (Ф4-re post-mortem, see _MAX_CONSECUTIVE_
    PLANNER_FALLBACKS docstring above for the live failure this fixes).

    An agent_turn step's raw output ({"text": ..., "executor": "agent_turn",
    ...}) is free-form narration, not a capability result — passed through
    unlabeled, the model reliably imitated that shallow shape in its NEXT
    plan JSON instead of the required {assumptions, steps, verification_
    plan}. Relabeled here so it reads unambiguously as "narration, not a
    schema example" and truncated to a short excerpt.

    Every other step's output is also capped: nothing bounded completed_
    steps' total size before, and a long exploratory run's own successful
    capability results (e.g. full page text from web_discover) can grow it
    without limit across dozens of replans just as easily.
    """
    if step.kind == "agent_turn":
        text = str((step.output or {}).get("text") or "")[:240]
        return {
            "kind": "agent_turn",
            "note": "free-form synthesis text, NOT a capability result — do not imitate this shape",
            "excerpt": text,
        }
    serialized = json.dumps(step.output or {}, ensure_ascii=False, default=str)
    if len(serialized) <= _MAX_STEP_OUTPUT_CHARS:
        return step.output
    return {"_truncated_output": serialized[:_MAX_STEP_OUTPUT_CHARS] + "...[truncated]"}


def _planner_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode()
    ).hexdigest()


async def _read_planner_snapshot(
    db: AsyncSession, work_order_id: uuid.UUID, *, lock_order: bool
) -> PlannerSnapshot | None:
    """Read every mutable DB input used by the planner under the order lock."""
    order = await db.get(WorkOrder, work_order_id, with_for_update=lock_order)
    if order is None or order.status not in {"received", "planning", "replanning"}:
        return None
    completed = list(
        (
            await db.execute(
                select(WorkStep)
                .where(WorkStep.work_order_id == order.id, WorkStep.state == "succeeded")
                .order_by(WorkStep.finished_at, WorkStep.id)
            )
        ).scalars()
    )
    completed_context = [
        {"step_key": step.step_key, "title": step.title, "output": _summarize_step_output(step)}
        for step in completed
    ]
    authority = {
        "work_order_id": str(order.id),
        "owner_key": order.owner_key,
        "status": order.status,
        "plan_revision": order.plan_revision,
        "objective": order.objective,
        "description": order.description,
        "constraints": order.constraints,
        "budgets": order.budgets,
        "metadata": order.metadata_,
        "blocker": order.blocker,
        "completed_steps": completed_context,
    }
    authority_digest = _planner_digest(authority)
    return PlannerSnapshot(
        work_order_id=order.id,
        owner_key=order.owner_key,
        status=order.status,
        plan_revision=order.plan_revision,
        objective=order.objective,
        description=order.description,
        constraints=dict(order.constraints or {}),
        budgets=dict(order.budgets or {}),
        metadata_=dict(order.metadata_ or {}),
        blocker=dict(order.blocker) if order.blocker else None,
        completed_context=completed_context,
        authority_digest=authority_digest,
        connector_hints=[],
        capability_catalog=[],
        digest=authority_digest,
    )


async def _freeze_planner_request(snapshot: PlannerSnapshot) -> PlannerSnapshot:
    """Freeze non-authoritative prompt enrichments into the dispatch digest."""
    connector_hints: list[dict[str, Any]] = []
    if is_exploratory(snapshot):
        from app.ai.connectors import find_connector_hints

        connector_hints = await find_connector_hints(snapshot.objective)
    capability_catalog = _planner_catalog()
    digest = _planner_digest(
        {
            "authority_digest": snapshot.authority_digest,
            "connector_hints": connector_hints,
            "capabilities": capability_catalog,
        }
    )
    return replace(
        snapshot,
        connector_hints=connector_hints,
        capability_catalog=capability_catalog,
        digest=digest,
    )


async def _apply_planning_result(
    db: AsyncSession,
    order: WorkOrder,
    *,
    planned: PlannedWork,
    use_model: bool,
    actor: str,
    fallback_reason: str | None,
    fallback_error: str | None = None,
) -> tuple[Any, list[WorkStep]]:
    metadata = dict(order.metadata_ or {})
    if fallback_reason is None:
        if "planner_fallback_streak" in metadata or "last_planner_error" in metadata:
            metadata.pop("planner_fallback_streak", None)
            metadata.pop("last_planner_error", None)
            order.metadata_ = metadata
    else:
        await append_event(
            db,
            order.id,
            "plan.fallback_used",
            actor=actor,
            payload={"error": str(fallback_error or fallback_reason)[:1000]},
        )
        if use_model:
            metadata["planner_fallback_streak"] = (
                int(metadata.get("planner_fallback_streak", 0)) + 1
            )
            metadata["last_planner_error"] = fallback_reason
            order.metadata_ = metadata

    plan, steps = await create_work_plan(
        db,
        order,
        steps=[step.model_dump() for step in planned.steps],
        assumptions=planned.assumptions,
        verification_plan=planned.verification_plan,
        actor=actor,
    )

    streak = int((order.metadata_ or {}).get("planner_fallback_streak", 0))
    if use_model and fallback_reason and streak >= _MAX_CONSECUTIVE_PLANNER_FALLBACKS:
        # Backstop: planner_error_context above didn't self-correct within
        # _MAX_CONSECUTIVE_PLANNER_FALLBACKS tries. This is a distinct,
        # honest, specific failure — the runtime's own schema contract kept
        # breaking, not the objective being hard — so stop spending the
        # remaining wall-clock/replan budget riding out an identical repeat
        # (this order's own pilot burned its last 53 minutes doing exactly
        # that) and report it as such instead of exhausting the budget
        # silently on a doomed loop.
        order.blocker = {
            "code": "planner_schema_failure_streak",
            "streak": streak,
            "last_error": fallback_reason,
        }
        if "blocked" in WORK_TRANSITIONS.get(order.status, frozenset()):
            await transition_work_order(
                db,
                order,
                "blocked",
                actor=actor,
                payload={"reason": "planner_schema_failure_streak", "streak": streak},
            )
    return plan, steps


async def plan_work_order(
    db: AsyncSession,
    order: WorkOrder,
    *,
    use_model: bool = True,
    actor: str = "capability-planner",
) -> tuple[Any, list[WorkStep]]:
    """Legacy in-transaction test/no-model seam.

    Runtime model callers use :func:`plan_work_order_detached`; keeping this
    helper preserves the established pure planner/fallback unit contracts.
    """
    completed = list(
        (
            await db.execute(
                select(WorkStep)
                .where(WorkStep.work_order_id == order.id, WorkStep.state == "succeeded")
                .order_by(WorkStep.finished_at, WorkStep.id)
            )
        ).scalars()
    )
    context = [
        {"step_key": step.step_key, "title": step.title, "output": _summarize_step_output(step)}
        for step in completed
    ]
    fallback_reason: str | None = None
    fallback_error: str | None = None
    try:
        planned = (
            await generate_capability_plan(
                order,
                completed_context=context,
                failure_context=order.blocker,
                planner_error_context=(order.metadata_ or {}).get("last_planner_error"),
            )
            if use_model
            else fallback_plan(order)
        )
    except Exception as exc:  # noqa: BLE001 - fallback is intentionally durable
        fallback_error = str(exc)
        fallback_reason = fallback_error[:500]
        planned = fallback_plan(order, reason=fallback_reason)
    return await _apply_planning_result(
        db,
        order,
        planned=planned,
        use_model=use_model,
        actor=actor,
        fallback_reason=fallback_reason,
        fallback_error=fallback_error,
    )


async def _planner_snapshot_is_current(factory: Any, snapshot: PlannerSnapshot) -> bool:
    async with factory() as db:
        current = await _read_planner_snapshot(db, snapshot.work_order_id, lock_order=True)
        return current is not None and current.authority_digest == snapshot.authority_digest


async def _persist_planner_stop(
    factory: Any, snapshot: PlannerSnapshot, stop: Any, *, actor: str
) -> bool:
    if stop.code == "planning_execution_already_started":
        # A concurrent loser must not overwrite the winner's planning state.
        return False
    async with factory() as db:
        current = await _read_planner_snapshot(db, snapshot.work_order_id, lock_order=True)
        if current is None or current.authority_digest != snapshot.authority_digest:
            return False
        order = await db.get(WorkOrder, snapshot.work_order_id)
        if order is None:
            return False
        order.blocker = stop.as_error()
        if "blocked" in WORK_TRANSITIONS.get(order.status, frozenset()):
            await transition_work_order(
                db,
                order,
                "blocked",
                actor=actor,
                payload={"reason": stop.code},
            )
        await append_event(
            db,
            order.id,
            "planning.budget_stopped",
            actor=actor,
            payload={"code": stop.code, "plan_revision": snapshot.plan_revision},
        )
        await db.commit()
        return True


async def _persist_planner_failure(
    factory: Any, snapshot: PlannerSnapshot, error: str, *, actor: str
) -> bool:
    """Count one failed planning attempt; block the order once the streak is spent."""
    async with factory() as db:
        current = await _read_planner_snapshot(db, snapshot.work_order_id, lock_order=True)
        if current is None or current.authority_digest != snapshot.authority_digest:
            return False
        order = await db.get(WorkOrder, snapshot.work_order_id)
        if order is None:
            return False
        reason = error[:500]
        metadata = dict(order.metadata_ or {})
        streak = int(metadata.get("planner_fallback_streak", 0)) + 1
        metadata["planner_fallback_streak"] = streak
        metadata["last_planner_error"] = reason
        order.metadata_ = metadata
        await append_event(
            db,
            order.id,
            "plan.planner_failed",
            actor=actor,
            payload={"error": error[:1000], "streak": streak},
        )
        if streak >= _MAX_CONSECUTIVE_PLANNER_FALLBACKS:
            order.blocker = {
                "code": "planner_schema_failure_streak",
                "streak": streak,
                "last_error": reason,
            }
            if "blocked" in WORK_TRANSITIONS.get(order.status, frozenset()):
                await transition_work_order(
                    db,
                    order,
                    "blocked",
                    actor=actor,
                    payload={"reason": "planner_schema_failure_streak", "streak": streak},
                )
        await db.commit()
        return True


async def plan_work_order_detached(
    work_order_id: uuid.UUID,
    *,
    session_factory: Any | None = None,
    actor: str = "capability-planner",
) -> bool:
    """Plan from a frozen snapshot without holding a caller transaction."""
    from app.ai.planner_budget_context import DetachedPlannerBudgetContext
    from app.ai.work_budget_context import BudgetExecutionStopped
    from app.db.session import _get_session_factory

    factory = session_factory or _get_session_factory()
    async with factory() as db:
        authority_snapshot = await _read_planner_snapshot(db, work_order_id, lock_order=True)
    if authority_snapshot is None:
        return False
    snapshot = authority_snapshot
    fallback_reason: str | None = None
    fallback_error: str | None = None
    try:
        snapshot = await _freeze_planner_request(authority_snapshot)
        # The once fence belongs to the authoritative DB snapshot, not to
        # best-effort prompt enrichments.  Two concurrent workers must not gain
        # separate dispatch licences merely because connector hints changed.
        scope = (
            f"{snapshot.work_order_id.hex}:r{snapshot.plan_revision}:"
            f"{snapshot.authority_digest[:24]}"
        )
        budget_context = DetachedPlannerBudgetContext(
            work_order_id=snapshot.work_order_id,
            owner_key=snapshot.owner_key,
            snapshot_digest=snapshot.authority_digest,
            prompt_digest=snapshot.digest,
            operation_scope=scope,
            session_factory=factory,
            snapshot_is_current=lambda: _planner_snapshot_is_current(factory, snapshot),
        )
        planned = await generate_capability_plan(
            snapshot,
            completed_context=snapshot.completed_context,
            failure_context=snapshot.blocker,
            planner_error_context=snapshot.metadata_.get("last_planner_error"),
            connector_hints=snapshot.connector_hints,
            capability_catalog=snapshot.capability_catalog,
            budget_context=budget_context,
        )
    except BudgetExecutionStopped as stop:
        await _persist_planner_stop(factory, snapshot, stop, actor=actor)
        return False
    except Exception as exc:  # noqa: BLE001 - a planner failure is recorded, not executed
        # The fallback plan was a single agent_turn step, which E21.2b5 retired
        # for headless work: every planner failure became a dead step, a failed
        # attempt and a replan that hit the same wall. Record the failure and
        # leave the order for the next dispatch tick; the streak backstop below
        # blocks it after _MAX_CONSECUTIVE_PLANNER_FALLBACKS attempts.
        await _persist_planner_failure(factory, snapshot, str(exc), actor=actor)
        return False

    async with factory() as db:
        current = await _read_planner_snapshot(db, work_order_id, lock_order=True)
        if current is None or current.authority_digest != snapshot.authority_digest:
            return False
        order = await db.get(WorkOrder, work_order_id)
        if order is None:
            return False
        await _apply_planning_result(
            db,
            order,
            planned=planned,
            use_model=True,
            actor=actor,
            fallback_reason=fallback_reason,
            fallback_error=fallback_error,
        )
        if order.status not in {"blocked", "failed"}:
            order.blocker = None
        await db.commit()
    return True


def _path_get(value: Any, path: str | None) -> Any:
    # Normalize bracket array indexing (items[0].url, the common JS/JSON-
    # path convention the model reliably reaches for) into this resolver's
    # own dot-segment form (items.0.url) — reuses the existing
    # isinstance(value, list) and segment.isdigit() branch below verbatim,
    # rather than teaching the segment loop a second index syntax. See the
    # _REF docstring comment above for why this exists.
    normalized_path = re.sub(r"\[(\d+)\]", r".\1", path or "")
    for segment in normalized_path.split("."):
        if not segment:
            continue
        if isinstance(value, dict):
            if segment in value:
                value = value[segment]
            elif isinstance(value.get("result"), dict) and segment in value["result"]:
                # Ф4-re (AGENT_AUTONOMY_ROADMAP.md): found live, TWICE now on
                # two independent exploratory pilots (dataflow_resolution_
                # error: 'supplier_id') — the model reliably writes
                # ${steps.X.output.<field>} instead of the actually-correct
                # ${steps.X.output.result.<field>} that _execute_capability's
                # {"result": ..., "result_summary": ..., "executor": ...}
                # wrapping requires. The base planner prompt already shows
                # this exact ".result." pattern in its own worked example —
                # recurring across independent live runs means this is a
                # predictable generalization gap in the model, not one-off
                # noise, so it's worth a bounded, one-level-deeper server-side
                # fallback rather than burning a replan on something the
                # runtime's own wrapping convention caused. Only kicks in
                # when the direct path is missing — never changes behaviour
                # for a correctly-written reference, and only unwraps one
                # level (the exact shape _execute_capability produces), not
                # an open-ended "guess the path" search.
                value = value["result"][segment]
            else:
                raise KeyError(segment)
        elif isinstance(value, list) and segment.isdigit():
            value = value[int(segment)]
        else:
            raise ValueError(f"dataflow path does not exist: {path}")
    return value


async def resolve_step_input(
    db: AsyncSession, step: WorkStep
) -> tuple[dict[str, Any], dict[str, Any]]:
    succeeded = list(
        (
            await db.execute(
                select(WorkStep)
                .where(WorkStep.work_order_id == step.work_order_id, WorkStep.state == "succeeded")
                .order_by(WorkStep.finished_at.desc())
            )
        ).scalars()
    )
    outputs: dict[str, Any] = {}
    for previous in succeeded:
        outputs.setdefault(previous.step_key, previous.output or {})
    provenance: dict[str, Any] = {}

    def resolve(value: Any, pointer: str) -> Any:
        if isinstance(value, str):
            match = _REF.fullmatch(value)
            if match:
                key, path = match.groups()
                if key not in outputs:
                    raise ValueError(f"dataflow source is not complete: {key}")
                resolved = _path_get(outputs[key], path)
                provenance[pointer] = {"step_key": key, "path": path or ""}
                return resolved

            def _inline(m: re.Match[str]) -> str:
                key, path = m.group(1), m.group(2)
                if key not in outputs:
                    raise ValueError(f"dataflow source is not complete: {key}")
                provenance[pointer] = {"step_key": key, "path": path or ""}
                resolved = _path_get(outputs[key], path)
                return "" if resolved is None else str(resolved)

            return _REF_INLINE.sub(_inline, value)
        if isinstance(value, list):
            return [resolve(item, f"{pointer}/{index}") for index, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: resolve(item, f"{pointer}/{key}") for key, item in value.items()}
        return value

    resolved_input = resolve(dict(step.input_ or {}), "")
    # Post-check: an unresolved placeholder must never reach a real tool call.
    # resolve() leaves unknown shapes untouched by design, which is how the
    # literal "${steps.…}" ended up as stored data; failing here sends the step
    # back to replanning instead.
    leftovers = _unresolved_refs(resolved_input)
    if leftovers:
        raise ValueError(
            "unresolved dataflow references remain: " + ", ".join(sorted(leftovers)[:5])
        )
    return resolved_input, provenance


def _unresolved_refs(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        if "${steps." in value:
            found.add(value[:120])
    elif isinstance(value, list):
        for item in value:
            found |= _unresolved_refs(item)
    elif isinstance(value, dict):
        for item in value.values():
            found |= _unresolved_refs(item)
    return found


def tool_call_digest(executor: str, capability: str | None, action: str | None, args: dict) -> str:
    import hashlib

    raw = json.dumps(
        {"executor": executor, "capability": capability, "action": action, "arguments": args},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(raw).hexdigest()
