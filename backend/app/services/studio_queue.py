"""Product-facing queue ledger for image studio jobs."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
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
        title=title or gen.prompt or gen.operation,
        priority=priority,
        generation_id=gen.id,
        meta={"operation": gen.operation},
    )
    db.add(job)
    await db.flush()
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
        title=title or run.name,
        priority=priority,
        lora_run_id=run.id,
        meta={"base_family": run.base_family},
    )
    db.add(job)
    await db.flush()
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


async def mark_job_done(db: AsyncSession, job: StudioJob | None) -> None:
    if not job:
        return
    job.status = StudioJobStatus.done
    job.finished_at = utcnow()
    job.error = None


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


async def mark_job_cancel_requested(db: AsyncSession, job: StudioJob | None) -> None:
    if not job or job.status in FINAL_STATUSES:
        return
    job.status = StudioJobStatus.cancel_requested
    job.cancel_requested_at = utcnow()


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


async def job_out(db: AsyncSession, job: StudioJob) -> dict[str, Any]:
    position = await queue_position(db, job)
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
        "eta_seconds": None,
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
        "meta": job.meta or {},
    }
