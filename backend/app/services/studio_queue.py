"""Product-facing queue ledger for image studio jobs."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ImageGeneration,
    ImageGenStatus,
    LoraTrainingRun,
    StudioJob,
    StudioJobKind,
    StudioJobStatus,
)

PENDING_STATUSES = {
    StudioJobStatus.queued,
    StudioJobStatus.waiting_resource,
}
ACTIVE_STATUSES = {
    StudioJobStatus.queued,
    StudioJobStatus.waiting_resource,
    StudioJobStatus.running,
    StudioJobStatus.cancel_requested,
}
FINAL_STATUSES = {
    StudioJobStatus.cancelled,
    StudioJobStatus.done,
    StudioJobStatus.failed,
}
CLEANUP_STATUSES = {
    StudioJobStatus.cancelled,
    StudioJobStatus.done,
}

CONTROL_REDIS_KEY = "studio:queue:control"
EVENT_REDIS_CHANNEL = "studio:queue:events"
MAX_ACTIVE_GLOBAL = 30
MAX_ACTIVE_PER_USER = 4
MAX_ACTIVE_PER_ADMIN = 12
MAX_TITLE_LEN = 300
DEFAULT_JOB_SECONDS = {
    StudioJobKind.image_generation: 180,
    StudioJobKind.lora_training: 8 * 3600,
}
_CONTROL_FALLBACK: dict[str, Any] = {}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _status_value(status: StudioJobStatus | str | None) -> str | None:
    if status is None:
        return None
    return status.value if hasattr(status, "value") else str(status)


def _image_status_value(status: ImageGenStatus | str | None) -> str | None:
    if status is None:
        return None
    return status.value if hasattr(status, "value") else str(status)


def _kind_value(kind: StudioJobKind | str) -> str:
    return kind.value if hasattr(kind, "value") else str(kind)


def _job_title(value: str | None, fallback: str) -> str:
    title = (value or fallback or "Задача").strip()
    if len(title) <= MAX_TITLE_LEN:
        return title
    return f"{title[: MAX_TITLE_LEN - 1].rstrip()}…"


def _control_defaults() -> dict[str, Any]:
    return {
        "paused": False,
        "drain": False,
        "reason": None,
        "updated_at": None,
        "updated_by": None,
    }


async def get_control_state() -> dict[str, Any]:
    try:
        import json

        from app.utils.redis_client import get_async_redis

        raw = await get_async_redis().get(CONTROL_REDIS_KEY)
        if raw:
            return {**_control_defaults(), **json.loads(raw)}
    except Exception:  # noqa: BLE001
        pass
    return {**_control_defaults(), **_CONTROL_FALLBACK}


async def set_control_state(
    *,
    paused: bool | None = None,
    drain: bool | None = None,
    reason: str | None = None,
    updated_by: str | None = None,
) -> dict[str, Any]:
    import json

    state = await get_control_state()
    if paused is not None:
        state["paused"] = paused
    if drain is not None:
        state["drain"] = drain
    state["reason"] = reason
    state["updated_at"] = utcnow().isoformat()
    state["updated_by"] = updated_by
    try:
        from app.utils.redis_client import get_async_redis

        await get_async_redis().set(CONTROL_REDIS_KEY, json.dumps(state, ensure_ascii=False))
        await publish_queue_event({"type": "control", "control": state})
    except Exception:  # noqa: BLE001
        _CONTROL_FALLBACK.clear()
        _CONTROL_FALLBACK.update(state)
    return state


async def publish_queue_event(event: dict[str, Any]) -> None:
    try:
        import json

        from app.utils.redis_client import get_async_redis

        payload = {**event, "ts": utcnow().isoformat()}
        await get_async_redis().publish(EVENT_REDIS_CHANNEL, json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        pass


async def active_count(
    db: AsyncSession,
    *,
    owner_sub: str | None = None,
    kind: StudioJobKind | None = None,
) -> int:
    q = select(func.count(StudioJob.id)).where(StudioJob.status.in_(ACTIVE_STATUSES))
    if owner_sub:
        q = q.where(StudioJob.owner_sub == owner_sub)
    if kind:
        q = q.where(StudioJob.kind == kind)
    return int((await db.execute(q)).scalar_one())


async def ensure_can_enqueue(
    db: AsyncSession,
    *,
    owner_sub: str | None,
    kind: StudioJobKind,
    roles: list[Any] | None = None,
) -> None:
    from fastapi import HTTPException

    state = await get_control_state()
    if state.get("paused"):
        raise HTTPException(503, state.get("reason") or "Очередь графической студии временно остановлена")
    if state.get("drain"):
        raise HTTPException(503, state.get("reason") or "Очередь в drain mode: новые задачи временно не принимаются")

    role_values = {getattr(role, "value", str(role)) for role in (roles or [])}
    per_user_limit = MAX_ACTIVE_PER_ADMIN if "admin" in role_values or "engineer" in role_values else MAX_ACTIVE_PER_USER
    owner_active = await active_count(db, owner_sub=owner_sub, kind=kind)
    if owner_active >= per_user_limit:
        raise HTTPException(
            429,
            f"Слишком много активных задач этого типа: {owner_active}/{per_user_limit}. Дождитесь завершения или отмените лишние.",
        )

    global_active = await active_count(db)
    if global_active >= MAX_ACTIVE_GLOBAL:
        raise HTTPException(
            503,
            f"Очередь перегружена: {global_active}/{MAX_ACTIVE_GLOBAL} активных задач. Попробуйте позже.",
        )


async def create_image_job(
    db: AsyncSession,
    gen: ImageGeneration,
    *,
    title: str | None = None,
    priority: int = 0,
) -> StudioJob:
    job = StudioJob(
        owner_sub=gen.owner_sub,
        kind=StudioJobKind.image_generation,
        status=StudioJobStatus.queued,
        resource="comfyui",
        title=_job_title(title or gen.prompt, gen.operation),
        priority=priority,
        generation_id=gen.id,
        meta={"operation": gen.operation},
    )
    db.add(job)
    await db.flush()
    await publish_queue_event({
        "type": "job_created",
        "job_id": str(job.id),
        "kind": _kind_value(job.kind),
        "owner_sub": job.owner_sub,
    })
    return job


async def create_lora_job(
    db: AsyncSession,
    run: LoraTrainingRun,
    *,
    title: str | None = None,
    priority: int = 0,
) -> StudioJob:
    job = StudioJob(
        owner_sub=run.owner_sub,
        kind=StudioJobKind.lora_training,
        status=StudioJobStatus.queued,
        resource="lora_training",
        title=_job_title(title, run.name),
        priority=priority,
        lora_run_id=run.id,
        meta={"base_family": run.base_family},
    )
    db.add(job)
    await db.flush()
    await publish_queue_event({
        "type": "job_created",
        "job_id": str(job.id),
        "kind": _kind_value(job.kind),
        "owner_sub": job.owner_sub,
    })
    return job


async def job_for_generation(db: AsyncSession, generation_id: uuid.UUID) -> StudioJob | None:
    return (
        await db.execute(
            select(StudioJob)
            .where(StudioJob.generation_id == generation_id)
            .order_by(StudioJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def job_for_lora_run(db: AsyncSession, run_id: uuid.UUID) -> StudioJob | None:
    return (
        await db.execute(
            select(StudioJob)
            .where(StudioJob.lora_run_id == run_id)
            .order_by(StudioJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def set_job_task_id(
    db: AsyncSession,
    job: StudioJob | None,
    task_id: str | None,
) -> None:
    if not job:
        return
    job.celery_task_id = task_id


async def mark_job_waiting(
    db: AsyncSession,
    job: StudioJob | None,
    *,
    reason: str,
) -> None:
    if not job or job.status in FINAL_STATUSES:
        return
    job.status = StudioJobStatus.waiting_resource
    job.error = reason[:2000]
    meta = dict(job.meta or {})
    meta["waiting_reason"] = reason[:500]
    job.meta = meta
    await publish_queue_event({
        "type": "job_updated",
        "job_id": str(job.id),
        "status": _status_value(job.status),
        "owner_sub": job.owner_sub,
    })


async def mark_job_running(
    db: AsyncSession,
    job: StudioJob | None,
    *,
    task_id: str | None = None,
) -> None:
    if not job or job.status in FINAL_STATUSES:
        return
    job.status = StudioJobStatus.running
    job.started_at = job.started_at or utcnow()
    job.error = None
    if task_id:
        job.celery_task_id = task_id
    await publish_queue_event({
        "type": "job_updated",
        "job_id": str(job.id),
        "status": _status_value(job.status),
        "owner_sub": job.owner_sub,
    })


async def mark_job_done(db: AsyncSession, job: StudioJob | None) -> None:
    if not job:
        return
    job.status = StudioJobStatus.done
    job.finished_at = utcnow()
    job.error = None
    await publish_queue_event({
        "type": "job_updated",
        "job_id": str(job.id),
        "status": _status_value(job.status),
        "owner_sub": job.owner_sub,
    })


async def mark_job_failed(
    db: AsyncSession,
    job: StudioJob | None,
    *,
    error: str,
) -> None:
    if not job:
        return
    job.status = StudioJobStatus.failed
    job.finished_at = utcnow()
    job.error = error[:2000]
    meta = dict(job.meta or {})
    meta["dead_letter_reason"] = job.error
    meta["failed_at"] = job.finished_at.isoformat()
    job.meta = meta
    await publish_queue_event({
        "type": "job_updated",
        "job_id": str(job.id),
        "status": _status_value(job.status),
        "owner_sub": job.owner_sub,
    })


async def mark_job_cancel_requested(db: AsyncSession, job: StudioJob | None) -> None:
    if not job or job.status in FINAL_STATUSES:
        return
    job.status = StudioJobStatus.cancel_requested
    job.cancel_requested_at = utcnow()
    await publish_queue_event({
        "type": "job_updated",
        "job_id": str(job.id),
        "status": _status_value(job.status),
        "owner_sub": job.owner_sub,
    })


async def mark_job_cancelled(
    db: AsyncSession,
    job: StudioJob | None,
    *,
    error: str | None = None,
) -> None:
    if not job:
        return
    job.status = StudioJobStatus.cancelled
    job.finished_at = utcnow()
    job.error = error
    await publish_queue_event({
        "type": "job_updated",
        "job_id": str(job.id),
        "status": _status_value(job.status),
        "owner_sub": job.owner_sub,
    })


async def queue_position(db: AsyncSession, job: StudioJob) -> int | None:
    if job.status not in PENDING_STATUSES:
        return None
    created_at = job.created_at or job.queued_at
    if created_at is None:
        return None
    count = (
        await db.execute(
            select(func.count(StudioJob.id)).where(
                StudioJob.resource == job.resource,
                StudioJob.status.in_(PENDING_STATUSES),
                or_(
                    StudioJob.priority > job.priority,
                    and_(
                        StudioJob.priority == job.priority,
                        StudioJob.created_at <= created_at,
                    ),
                ),
            )
        )
    ).scalar_one()
    return int(count)


async def estimate_eta_seconds(db: AsyncSession, job: StudioJob) -> int | None:
    position = await queue_position(db, job)
    if not position:
        return None
    default_seconds = DEFAULT_JOB_SECONDS.get(job.kind, 300)
    recent = (
        await db.execute(
            select(StudioJob.started_at, StudioJob.finished_at)
            .where(
                StudioJob.kind == job.kind,
                StudioJob.status == StudioJobStatus.done,
                StudioJob.started_at.is_not(None),
                StudioJob.finished_at.is_not(None),
            )
            .order_by(StudioJob.finished_at.desc())
            .limit(20)
        )
    ).all()
    durations: list[float] = []
    for started_at, finished_at in recent:
        try:
            durations.append(max(1.0, (finished_at - started_at).total_seconds()))
        except Exception:  # noqa: BLE001
            pass
    avg = sum(durations) / len(durations) if durations else default_seconds
    running_count = int(
        (
            await db.execute(
                select(func.count(StudioJob.id)).where(
                    StudioJob.resource == job.resource,
                    StudioJob.status == StudioJobStatus.running,
                )
            )
        ).scalar_one()
    )
    if job.status == StudioJobStatus.running and job.started_at:
        elapsed = max(0, int((utcnow() - job.started_at).total_seconds()))
        return max(0, int(avg - elapsed))
    effective_position = max(0, position - max(running_count, 1))
    return int(effective_position * avg)


async def active_count_for_owner(
    db: AsyncSession,
    *,
    owner_sub: str | None,
    kind: StudioJobKind,
) -> int:
    if not owner_sub:
        return 0
    count = (
        await db.execute(
            select(func.count(StudioJob.id)).where(
                StudioJob.owner_sub == owner_sub,
                StudioJob.kind == kind,
                StudioJob.status.in_(ACTIVE_STATUSES),
            )
        )
    ).scalar_one()
    return int(count)


async def retry_failed_job(db: AsyncSession, job: StudioJob) -> None:
    if job.status != StudioJobStatus.failed:
        from fastapi import HTTPException

        raise HTTPException(409, "Повторить можно только failed-задачу")
    meta = dict(job.meta or {})
    attempts = int(meta.get("retry_attempts") or 0)
    if attempts >= 3:
        from fastapi import HTTPException

        raise HTTPException(409, "Лимит повторов исчерпан")
    meta["retry_attempts"] = attempts + 1
    meta["retried_at"] = utcnow().isoformat()
    job.meta = meta
    job.status = StudioJobStatus.queued
    job.queued_at = utcnow()
    job.started_at = None
    job.finished_at = None
    job.error = None
    await publish_queue_event({
        "type": "job_updated",
        "job_id": str(job.id),
        "status": _status_value(job.status),
        "owner_sub": job.owner_sub,
    })


async def cleanup_terminal_jobs(db: AsyncSession) -> int:
    """Remove queue rows that should no longer be visible in the product queue.

    Failed jobs stay as dead-letter records so operators can inspect/retry them.
    Successful and cancelled jobs are already represented by the linked
    ImageGeneration/LoraTrainingRun status, so keeping them in the queue makes
    the UI look stuck.
    """
    result = await db.execute(delete(StudioJob).where(StudioJob.status.in_(CLEANUP_STATUSES)))
    deleted = int(result.rowcount or 0)
    if deleted:
        await publish_queue_event({"type": "jobs_cleaned", "deleted": deleted})
    return deleted


async def bulk_cancel_pending(
    db: AsyncSession,
    *,
    resource: str | None = None,
    owner_sub: str | None = None,
) -> int:
    q = select(StudioJob).where(StudioJob.status.in_(PENDING_STATUSES))
    if resource:
        q = q.where(StudioJob.resource == resource)
    if owner_sub:
        q = q.where(StudioJob.owner_sub == owner_sub)
    rows = (await db.execute(q)).scalars().all()
    for job in rows:
        await mark_job_cancelled(db, job, error="Задача отменена оператором.")
    return len(rows)


async def queue_stats(db: AsyncSession) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(StudioJob.resource, StudioJob.kind, StudioJob.status, func.count(StudioJob.id))
            .group_by(StudioJob.resource, StudioJob.kind, StudioJob.status)
        )
    ).all()
    by_resource: dict[str, dict[str, int]] = {}
    by_kind: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for resource, kind, status, count in rows:
        status_value = _status_value(status) or "unknown"
        kind_value = _kind_value(kind)
        by_resource.setdefault(resource, {})[status_value] = int(count)
        by_kind.setdefault(kind_value, {})[status_value] = int(count)
        totals[status_value] = totals.get(status_value, 0) + int(count)

    active = sum(totals.get(_status_value(status) or "", 0) for status in ACTIVE_STATUSES)
    completed = (
        await db.execute(
            select(StudioJob.started_at, StudioJob.finished_at, StudioJob.queued_at)
            .where(StudioJob.finished_at >= utcnow() - timedelta(hours=24))
            .where(StudioJob.finished_at.is_not(None))
            .limit(500)
        )
    ).all()
    waits: list[float] = []
    runtimes: list[float] = []
    for started_at, finished_at, queued_at in completed:
        if started_at and queued_at:
            waits.append(max(0.0, (started_at - queued_at).total_seconds()))
        if started_at and finished_at:
            runtimes.append(max(0.0, (finished_at - started_at).total_seconds()))

    def avg(values: list[float]) -> int | None:
        return int(sum(values) / len(values)) if values else None

    return {
        "control": await get_control_state(),
        "limits": {
            "global_active": MAX_ACTIVE_GLOBAL,
            "per_user_active": MAX_ACTIVE_PER_USER,
            "operator_active": MAX_ACTIVE_PER_ADMIN,
        },
        "totals": totals,
        "active": active,
        "by_resource": by_resource,
        "by_kind": by_kind,
        "avg_wait_seconds_24h": avg(waits),
        "avg_runtime_seconds_24h": avg(runtimes),
    }


async def job_out(db: AsyncSession, job: StudioJob) -> dict[str, Any]:
    position = await queue_position(db, job)
    eta_seconds = await estimate_eta_seconds(db, job)
    linked_status: str | None = None
    linked_progress: dict[str, Any] = dict(job.progress or {})
    linked_error = job.error

    if job.generation_id:
        gen = await db.get(ImageGeneration, job.generation_id)
        if gen:
            linked_status = _image_status_value(gen.status)
            linked_error = linked_error or gen.error
            try:
                from app.tasks.image_generation import read_progress

                progress = read_progress(str(gen.id))
                if progress:
                    linked_progress = progress
            except Exception:  # noqa: BLE001
                pass
    elif job.lora_run_id:
        run = await db.get(LoraTrainingRun, job.lora_run_id)
        if run:
            linked_status = run.status.value if hasattr(run.status, "value") else str(run.status)
            linked_progress = dict(run.progress or linked_progress or {})
            linked_error = linked_error or run.error

    return {
        "id": str(job.id),
        "kind": job.kind.value if hasattr(job.kind, "value") else str(job.kind),
        "status": _status_value(job.status),
        "resource": job.resource,
        "title": job.title,
        "priority": job.priority,
        "position": position,
        "eta_seconds": eta_seconds,
        "owner_sub": job.owner_sub,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "queued_at": job.queued_at.isoformat() if job.queued_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "cancel_requested_at": (
            job.cancel_requested_at.isoformat() if job.cancel_requested_at else None
        ),
        "generation_id": str(job.generation_id) if job.generation_id else None,
        "lora_run_id": str(job.lora_run_id) if job.lora_run_id else None,
        "linked_status": linked_status,
        "progress": linked_progress or None,
        "error": linked_error,
        "can_cancel": job.status in ACTIVE_STATUSES,
        "can_retry": job.status == StudioJobStatus.failed and int((job.meta or {}).get("retry_attempts") or 0) < 3,
        "meta": job.meta or {},
    }
