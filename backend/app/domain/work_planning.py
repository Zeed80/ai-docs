"""Capability-grounded planning, dataflow resolution, and bounded replanning."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.capability_manifest import CapabilityDefinition, load_capability_manifest
from app.db.models import WorkOrder, WorkStep
from app.domain.work_orders import append_event, create_work_plan

_REF = re.compile(r"^\$\{steps\.([a-zA-Z0-9_-]+)\.output(?:\.([a-zA-Z0-9_.-]+))?\}$")


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
    kind: str = Field(pattern="^(capability|agent_turn|decompose)$")
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

    @model_validator(mode="after")
    def executor_is_complete(self) -> "PlannedStep":
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


def _action_names(capability: CapabilityDefinition) -> set[str]:
    action = (capability.parameters.get("properties") or {}).get("action") or {}
    description = str(action.get("description") or "")
    return {
        token.strip()
        for token in re.split(r"\s*[|,]\s*", description)
        if re.fullmatch(r"[a-z][a-z0-9_]*", token.strip())
    }


def validate_capability_plan(plan: PlannedWork) -> PlannedWork:
    manifest = load_capability_manifest()
    capabilities = manifest.by_name
    keys = {step.step_key for step in plan.steps}
    if len(keys) != len(plan.steps):
        raise ValueError("planner returned duplicate step keys")
    for step in plan.steps:
        unknown = set(step.depends_on) - keys
        if unknown or step.step_key in step.depends_on:
            raise ValueError(f"invalid dependencies for {step.step_key}: {sorted(unknown)}")
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
    order: WorkOrder,
    *,
    completed_context: list[dict[str, Any]] | None = None,
    failure_context: dict[str, Any] | None = None,
) -> PlannedWork:
    """Ask the reasoning model for a bounded DAG grounded in the live manifest."""
    from app.ai.ollama_client import generate_json
    from app.ai.model_resolver import get_reasoning_model

    model_config = get_reasoning_model()
    prompt = json.dumps(
        {
            "objective": order.objective,
            "description": order.description,
            "constraints": order.constraints,
            "budgets": order.budgets,
            "new_instructions": (order.metadata_ or {}).get("instructions", []),
            "completed_steps": completed_context or [],
            "last_failure": failure_context,
            "capabilities": _planner_catalog(),
        },
        ensure_ascii=False,
        default=str,
    )
    system = """You are a durable task planner. Return JSON only.
Build the smallest executable DAG using only listed capability/action pairs. Each external
operation is one capability step. Use agent_turn only for synthesis/reasoning that cannot be
done by a capability. Pass data between steps with exact references like
${steps.lookup.output.result.items}. Never repeat completed work during replanning.
Schema: {assumptions:[string], steps:[{step_key,title,kind,capability?,action?,input,
depends_on,success_predicate,risk_level,max_attempts,timeout_seconds}],
verification_plan:{mode,checks}}. Gated actions must be high risk."""
    raw = await generate_json(
        prompt,
        model=model_config.model,
        provider=model_config.provider,
        system=system,
        temperature=0.0,
        max_tokens=8192,
        timeout_seconds=180,
    )
    try:
        return validate_capability_plan(PlannedWork.model_validate(raw))
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


async def plan_work_order(
    db: AsyncSession,
    order: WorkOrder,
    *,
    use_model: bool = True,
    actor: str = "capability-planner",
) -> tuple[Any, list[WorkStep]]:
    completed = list(
        (
            await db.execute(
                select(WorkStep)
                .where(WorkStep.work_order_id == order.id, WorkStep.state == "succeeded")
                .order_by(WorkStep.finished_at)
            )
        ).scalars()
    )
    context = [
        {"step_key": step.step_key, "title": step.title, "output": step.output}
        for step in completed
    ]
    failure = order.blocker
    try:
        planned = (
            await generate_capability_plan(
                order, completed_context=context, failure_context=failure
            )
            if use_model
            else fallback_plan(order)
        )
    except Exception as exc:  # noqa: BLE001 - fallback is intentionally durable
        planned = fallback_plan(order, reason=str(exc)[:500])
        await append_event(
            db,
            order.id,
            "plan.fallback_used",
            actor=actor,
            payload={"error": str(exc)[:1000]},
        )
    return await create_work_plan(
        db,
        order,
        steps=[step.model_dump() for step in planned.steps],
        assumptions=planned.assumptions,
        verification_plan=planned.verification_plan,
        actor=actor,
    )


def _path_get(value: Any, path: str | None) -> Any:
    for segment in (path or "").split("."):
        if not segment:
            continue
        if isinstance(value, dict):
            value = value[segment]
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
            return value
        if isinstance(value, list):
            return [resolve(item, f"{pointer}/{index}") for index, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: resolve(item, f"{pointer}/{key}") for key, item in value.items()}
        return value

    return resolve(dict(step.input_ or {}), ""), provenance


def tool_call_digest(executor: str, capability: str | None, action: str | None, args: dict) -> str:
    import hashlib

    raw = json.dumps(
        {"executor": executor, "capability": capability, "action": action, "arguments": args},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(raw).hexdigest()
