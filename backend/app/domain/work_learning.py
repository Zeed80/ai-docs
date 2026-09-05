"""Derive reusable, provenance-backed memory from verified WorkOrders."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    MemoryFact,
    WorkAcceptanceCriterion,
    WorkEvidence,
    WorkLearning,
    WorkOrder,
    WorkPlan,
    WorkStep,
    WorkStepAttempt,
    WorkToolCall,
)
from app.domain.work_orders import append_event

RecipeRecorder = Callable[..., Awaitable[tuple[bool, str | None]]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _lesson_summary(
    order: WorkOrder,
    steps: list[WorkStep],
    attempts: list[WorkStepAttempt],
    tool_calls: list[WorkToolCall],
) -> tuple[str, list[dict[str, Any]]]:
    capabilities = [
        f"{call.capability}.{call.action}"
        for call in tool_calls
        if call.capability and call.action and call.status == "succeeded"
    ]
    retry_count = sum(max(0, step.attempt_count - 1) for step in steps)
    failed_attempts = sum(1 for attempt in attempts if attempt.status in {"failed", "abandoned"})
    lessons: list[dict[str, Any]] = [
        {
            "kind": "verified_outcome",
            "statement": "Поручение завершено только после прохождения критериев приёмки.",
            "value": True,
        },
        {
            "kind": "execution_shape",
            "steps": len(steps),
            "tool_calls": len(tool_calls),
            "capabilities": capabilities,
        },
    ]
    if retry_count or failed_attempts:
        lessons.append(
            {
                "kind": "recovery",
                "retries": retry_count,
                "failed_or_abandoned_attempts": failed_attempts,
            }
        )
    if order.plan_revision > 1:
        lessons.append({"kind": "replanning", "plan_revisions": order.plan_revision})
    summary = (order.result_summary or "").strip()
    if not summary:
        summary = f"Поручение «{order.objective[:300]}» выполнено и проверено."
    return summary[:8000], lessons


async def _load_trace(db: AsyncSession, order_id: uuid.UUID) -> dict[str, Any]:
    order = await db.get(WorkOrder, order_id)
    if order is None:
        raise ValueError("work_order_not_found")
    if order.status != "completed":
        raise ValueError(f"work_order_not_completed:{order.status}")
    plans = list(
        (
            await db.execute(
                select(WorkPlan)
                .where(WorkPlan.work_order_id == order.id)
                .order_by(WorkPlan.revision)
            )
        ).scalars()
    )
    steps = list(
        (
            await db.execute(
                select(WorkStep)
                .where(WorkStep.work_order_id == order.id)
                .order_by(WorkStep.created_at)
            )
        ).scalars()
    )
    step_ids = [step.id for step in steps]
    attempts = (
        list(
            (
                await db.execute(
                    select(WorkStepAttempt)
                    .where(WorkStepAttempt.step_id.in_(step_ids))
                    .order_by(WorkStepAttempt.started_at)
                )
            ).scalars()
        )
        if step_ids
        else []
    )
    calls = list(
        (
            await db.execute(
                select(WorkToolCall)
                .where(WorkToolCall.work_order_id == order.id)
                .order_by(WorkToolCall.created_at)
            )
        ).scalars()
    )
    criteria = list(
        (
            await db.execute(
                select(WorkAcceptanceCriterion)
                .where(WorkAcceptanceCriterion.work_order_id == order.id)
                .order_by(WorkAcceptanceCriterion.created_at)
            )
        ).scalars()
    )
    evidence = list(
        (
            await db.execute(
                select(WorkEvidence)
                .where(WorkEvidence.work_order_id == order.id)
                .order_by(WorkEvidence.created_at)
            )
        ).scalars()
    )
    return {
        "order": order,
        "plans": plans,
        "steps": steps,
        "attempts": attempts,
        "calls": calls,
        "criteria": criteria,
        "evidence": evidence,
    }


async def process_work_learning(
    work_order_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    recipe_recorder: RecipeRecorder | None = None,
) -> bool:
    """Idempotently materialize memory and a safe draft recipe for an order."""
    from app.db.session import _get_session_factory

    factory = session_factory or _get_session_factory()
    async with factory() as db:
        learning = (
            await db.execute(
                select(WorkLearning)
                .where(WorkLearning.work_order_id == work_order_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if learning is None:
            order = await db.get(WorkOrder, work_order_id)
            if order is None or order.status != "completed":
                return False
            learning = WorkLearning(work_order_id=work_order_id, status="pending")
            db.add(learning)
            await db.flush()
        if learning.status == "recorded":
            return True
        if learning.status == "processing" and learning.updated_at > _utcnow() - timedelta(
            minutes=5
        ):
            return False
        learning.status = "processing"
        learning.extraction_attempts += 1
        learning.last_error = None
        await db.commit()

    try:
        async with factory() as db:
            trace = await _load_trace(db, work_order_id)
            order: WorkOrder = trace["order"]
            steps: list[WorkStep] = trace["steps"]
            attempts: list[WorkStepAttempt] = trace["attempts"]
            calls: list[WorkToolCall] = trace["calls"]
            summary, lessons = _lesson_summary(order, steps, attempts, calls)
            provenance = {
                "work_order_id": str(order.id),
                "owner_key": order.owner_key,
                "source": order.source,
                "plan_revisions": [plan.revision for plan in trace["plans"]],
                "tool_calls": [
                    {
                        "id": str(call.id),
                        "digest": call.action_digest,
                        "status": call.status,
                    }
                    for call in calls
                ],
                "criteria": [
                    {
                        "id": str(criterion.id),
                        "key": criterion.criterion_key,
                        "status": criterion.status,
                        "verified_by": criterion.verified_by,
                    }
                    for criterion in trace["criteria"]
                ],
                "evidence": [
                    {
                        "id": str(item.id),
                        "type": item.evidence_type,
                        "hash": item.content_hash,
                        "verifier_status": item.verifier_status,
                    }
                    for item in trace["evidence"]
                ],
                "verified_at": order.completed_at.isoformat() if order.completed_at else None,
            }
            learning = (
                await db.execute(
                    select(WorkLearning)
                    .where(WorkLearning.work_order_id == work_order_id)
                    .with_for_update()
                )
            ).scalar_one()
            learning.summary = summary
            learning.lessons = lessons
            learning.provenance = provenance
            if learning.memory_fact_id is None:
                subject_key = hashlib.sha256(
                    " ".join(order.objective.casefold().split()).encode()
                ).hexdigest()
                previous = (
                    await db.execute(
                        select(MemoryFact)
                        .where(
                            MemoryFact.scope == f"owner:{order.owner_key}",
                            MemoryFact.kind == "work_order_lesson",
                            MemoryFact.status == "active",
                            MemoryFact.metadata_["subject_key"].as_string() == subject_key,
                        )
                        .order_by(MemoryFact.last_verified_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if previous is not None and previous.summary == summary:
                    previous.last_verified_at = order.completed_at or _utcnow()
                    previous.expires_at = _utcnow() + timedelta(days=180)
                    previous.provenance = provenance
                    metadata = dict(previous.metadata_ or {})
                    confirmations = list(metadata.get("confirming_work_orders") or [])
                    if str(order.id) not in confirmations:
                        confirmations.append(str(order.id))
                    metadata["confirming_work_orders"] = confirmations[-20:]
                    previous.metadata_ = metadata
                    learning.memory_fact_id = previous.id
                else:
                    fact = MemoryFact(
                        scope=f"owner:{order.owner_key}",
                        kind="work_order_lesson",
                        title=f"Проверенный опыт: {order.objective[:430]}",
                        summary=summary,
                        source="work_order",
                        confidence=1.0,
                        pinned=False,
                        last_verified_at=order.completed_at or _utcnow(),
                        valid_from=order.completed_at or _utcnow(),
                        expires_at=_utcnow() + timedelta(days=180),
                        status="active",
                        provenance=provenance,
                        metadata_={
                            "work_order_id": str(order.id),
                            "subject_key": subject_key,
                            "lessons": lessons,
                            "provenance": provenance,
                            "conflicts_with": (str(previous.id) if previous is not None else None),
                        },
                    )
                    db.add(fact)
                    await db.flush()
                    if previous is not None:
                        previous.status = "superseded"
                        previous.superseded_by_id = fact.id
                        previous.expires_at = _utcnow()
                    learning.memory_fact_id = fact.id
            await db.commit()

        recipe_steps = [
            {
                "capability": call.capability,
                "action": call.action,
                "args_template": call.arguments,
            }
            for call in calls
            if call.executor == "capability"
            and call.status == "succeeded"
            and call.capability
            and call.action
        ]
        recipe_results = [
            call.output
            for call in calls
            if call.executor == "capability"
            and call.status == "succeeded"
            and call.capability
            and call.action
        ]
        if recipe_recorder is None:
            from app.ai.recipes import record_candidate_with_id

            recipe_recorder = record_candidate_with_id
        recorded, recipe_id = await recipe_recorder(
            user_text=order.objective,
            role=str((order.metadata_ or {}).get("role") or "autonomous_worker"),
            intent=str((order.metadata_ or {}).get("intent") or order.source),
            steps=recipe_steps,
            step_results=recipe_results,
            session_id=str(order.id),
            output_channel=str((order.metadata_ or {}).get("output_channel") or "work_order"),
        )

        async with factory() as db:
            learning = (
                await db.execute(
                    select(WorkLearning)
                    .where(WorkLearning.work_order_id == work_order_id)
                    .with_for_update()
                )
            ).scalar_one()
            if recorded and recipe_id:
                learning.recipe_skill_id = uuid.UUID(recipe_id)
            learning.status = "recorded"
            learning.processed_at = _utcnow()
            await append_event(
                db,
                work_order_id,
                "work.learning_recorded",
                actor="work-learning",
                payload={
                    "memory_fact_id": str(learning.memory_fact_id),
                    "recipe_skill_id": str(learning.recipe_skill_id)
                    if learning.recipe_skill_id
                    else None,
                    "recipe_recorded": bool(recorded),
                },
            )
            await db.commit()
        return True
    except Exception as exc:
        async with factory() as db:
            learning = (
                await db.execute(
                    select(WorkLearning)
                    .where(WorkLearning.work_order_id == work_order_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if learning is not None:
                learning.status = "failed"
                learning.last_error = {
                    "type": type(exc).__name__,
                    "message": str(exc)[:1000],
                }
                await db.commit()
        return False


async def reset_work_learning(db: AsyncSession, work_order_id: uuid.UUID) -> WorkLearning | None:
    learning = (
        await db.execute(
            select(WorkLearning)
            .where(WorkLearning.work_order_id == work_order_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if learning is None:
        return None
    learning.status = "pending"
    learning.last_error = None
    await db.flush()
    return learning


async def expire_stale_work_memory(
    *, session_factory: async_sessionmaker[AsyncSession] | None = None
) -> int:
    """Soft-expire learned facts so retrieval cannot present stale truth."""
    from app.db.session import _get_session_factory

    factory = session_factory or _get_session_factory()
    async with factory() as db:
        result = await db.execute(
            update(MemoryFact)
            .where(
                MemoryFact.kind == "work_order_lesson",
                MemoryFact.status == "active",
                MemoryFact.expires_at.is_not(None),
                MemoryFact.expires_at <= _utcnow(),
            )
            .values(status="stale")
        )
        await db.commit()
        return int(result.rowcount or 0)
