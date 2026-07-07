"""Unified queue API for the graphic studio."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, UserRole
from app.db.models import (
    ImageGeneration,
    ImageGenStatus,
    LoraRunStatus,
    StudioJob,
    StudioJobKind,
    StudioJobStatus,
)
from app.db.session import get_db
from app.services import studio_queue

router = APIRouter()
logger = structlog.get_logger()


class PriorityPatch(BaseModel):
    priority: int = Field(ge=-100, le=100)


class QueueControlPatch(BaseModel):
    paused: bool | None = None
    drain: bool | None = None
    reason: str | None = Field(default=None, max_length=500)


class BulkCancelBody(BaseModel):
    resource: str | None = Field(default=None, max_length=80)
    owner_sub: str | None = Field(default=None, max_length=255)


def _is_agent_service(user: UserInfo) -> bool:
    return user.sub == "agent-service"


def _is_admin(user: UserInfo) -> bool:
    return UserRole.admin in (user.roles or [])


def _can_see_all(user: UserInfo) -> bool:
    return _is_admin(user) or UserRole.engineer in (user.roles or []) or _is_agent_service(user)


def _can_manage_queue(user: UserInfo) -> bool:
    return _is_admin(user) or UserRole.engineer in (user.roles or []) or _is_agent_service(user)


def _can_access(job: StudioJob | None, user: UserInfo) -> bool:
    return job is not None and (job.owner_sub == user.sub or _can_see_all(user))


@router.get("/queue")
async def list_queue(
    status: str | None = None,
    kind: str | None = None,
    mine: bool = False,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    if await studio_queue.cleanup_terminal_jobs(db):
        await db.commit()
    q = select(StudioJob)
    if mine or not _can_see_all(user):
        q = q.where(StudioJob.owner_sub == user.sub)
    if status:
        statuses: list[StudioJobStatus] = []
        for item in status.split(","):
            try:
                statuses.append(StudioJobStatus(item.strip()))
            except ValueError:
                raise HTTPException(400, f"Неизвестный статус очереди: {item}") from None
        q = q.where(StudioJob.status.in_(statuses))
    if kind:
        try:
            q = q.where(StudioJob.kind == StudioJobKind(kind))
        except ValueError:
            raise HTTPException(400, f"Неизвестный тип задачи: {kind}") from None
    rows = (
        await db.execute(
            q.order_by(
                StudioJob.status.in_(
                    [StudioJobStatus.queued, StudioJobStatus.waiting_resource, StudioJobStatus.running]
                ).desc(),
                StudioJob.priority.desc(),
                StudioJob.created_at.asc(),
            )
            .limit(min(limit, 300))
            .offset(max(offset, 0))
        )
    ).scalars().all()
    return {"items": [await studio_queue.job_out(db, row) for row in rows]}


@router.get("/queue/stats")
async def queue_stats(
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    if not _can_manage_queue(user):
        raise HTTPException(403, "Недостаточно прав для просмотра метрик очереди")
    if await studio_queue.cleanup_terminal_jobs(db):
        await db.commit()
    return await studio_queue.queue_stats(db)


@router.get("/queue/control")
async def get_queue_control(
    user: UserInfo = Depends(get_current_user),
) -> dict:
    if not _can_manage_queue(user):
        raise HTTPException(403, "Недостаточно прав для управления очередью")
    return await studio_queue.get_control_state()


@router.patch("/queue/control")
async def patch_queue_control(
    body: QueueControlPatch,
    user: UserInfo = Depends(get_current_user),
) -> dict:
    if not _can_manage_queue(user):
        raise HTTPException(403, "Недостаточно прав для управления очередью")
    return await studio_queue.set_control_state(
        paused=body.paused,
        drain=body.drain,
        reason=body.reason,
        updated_by=user.sub,
    )


@router.post("/queue/bulk-cancel")
async def bulk_cancel_queue(
    body: BulkCancelBody,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    if not _can_manage_queue(user):
        raise HTTPException(403, "Недостаточно прав для управления очередью")
    cancelled = await studio_queue.bulk_cancel_pending(
        db,
        resource=body.resource,
        owner_sub=body.owner_sub,
    )
    await db.commit()
    return {"cancelled": cancelled}


@router.get("/queue/events")
async def queue_events(
    user: UserInfo = Depends(get_current_user),
) -> StreamingResponse:
    see_all = _can_see_all(user)

    async def stream() -> AsyncIterator[str]:
        import json

        from app.services.studio_queue import EVENT_REDIS_CHANNEL
        from app.utils.redis_client import get_async_redis

        redis = get_async_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(EVENT_REDIS_CHANNEL)
        yield "event: ready\ndata: {}\n\n"
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data") or "{}"
                try:
                    payload = json.loads(data)
                except Exception:  # noqa: BLE001
                    payload = {"type": "unknown"}
                if not see_all:
                    if payload.get("type") == "control":
                        continue
                    if payload.get("owner_sub") != user.sub:
                        continue
                data = json.dumps(payload, ensure_ascii=False)
                yield f"event: queue\ndata: {data}\n\n"
        finally:
            await pubsub.unsubscribe(EVENT_REDIS_CHANNEL)
            await pubsub.close()

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    job = await db.get(StudioJob, job_id)
    if not _can_access(job, user):
        raise HTTPException(404, "Задача не найдена")
    return await studio_queue.job_out(db, job)


@router.post("/queue/{job_id}/cancel")
async def cancel_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    job = await db.get(StudioJob, job_id)
    if not _can_access(job, user):
        raise HTTPException(404, "Задача не найдена")
    if job.status in studio_queue.FINAL_STATUSES:
        return await studio_queue.job_out(db, job)

    from app.tasks.celery_app import celery_app

    if job.celery_task_id and job.status in {StudioJobStatus.queued, StudioJobStatus.waiting_resource}:
        try:
            celery_app.control.revoke(job.celery_task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("studio_queue_revoke_failed", job_id=str(job.id), error=str(exc)[:160])

    if job.generation_id:
        from app.ai.comfyui_client import ComfyUIClient

        gen = await db.get(ImageGeneration, job.generation_id)
        if gen:
            gen.status = ImageGenStatus.cancelled
            gen.error = "Задача отменена пользователем."
        if job.status == StudioJobStatus.running:
            try:
                await ComfyUIClient.from_registry().interrupt()
            except Exception as exc:  # noqa: BLE001
                logger.warning("studio_queue_comfyui_interrupt_failed", job_id=str(job.id), error=str(exc)[:160])
        await studio_queue.mark_job_cancelled(db, job, error="Задача отменена пользователем.")

    elif job.lora_run_id:
        from app.ai import gpu_lock
        from app.db.models import LoraTrainingRun

        run = await db.get(LoraTrainingRun, job.lora_run_id)
        if run:
            if run.status == LoraRunStatus.queued:
                run.status = LoraRunStatus.cancelled
                await studio_queue.mark_job_cancelled(db, job, error="Задача отменена пользователем.")
            elif run.status == LoraRunStatus.running:
                run.status = LoraRunStatus.stopping
                await studio_queue.mark_job_cancel_requested(db, job)
                try:
                    gpu_lock.request_stop(str(run.id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("studio_queue_lora_stop_failed", job_id=str(job.id), error=str(exc)[:160])
            else:
                await studio_queue.mark_job_cancelled(db, job, error="Задача отменена пользователем.")
        else:
            await studio_queue.mark_job_cancelled(db, job, error="Связанный LoRA-запуск не найден.")

    await db.commit()
    await db.refresh(job)
    return await studio_queue.job_out(db, job)


@router.post("/queue/{job_id}/retry")
async def retry_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    if not _can_manage_queue(user):
        raise HTTPException(403, "Недостаточно прав для повторного запуска")
    job = await db.get(StudioJob, job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    await studio_queue.ensure_can_enqueue(
        db,
        owner_sub=job.owner_sub,
        kind=job.kind,
        roles=user.roles,
    )
    await studio_queue.retry_failed_job(db, job)

    from app.tasks.celery_app import celery_app

    if job.generation_id:
        gen = await db.get(ImageGeneration, job.generation_id)
        if gen:
            gen.status = ImageGenStatus.queued
            gen.error = None
        task = celery_app.send_task("image_generation.run_image_generation", args=[str(job.generation_id)], queue="studio")
        job.celery_task_id = task.id
        if gen:
            gen.celery_task_id = task.id
    elif job.lora_run_id:
        from app.db.models import LoraTrainingRun

        run = await db.get(LoraTrainingRun, job.lora_run_id)
        if run:
            run.status = LoraRunStatus.queued
            run.error = None
        task = celery_app.send_task("lora.run_training", args=[str(job.lora_run_id)])
        job.celery_task_id = task.id
        if run:
            run.celery_task_id = task.id
    await db.commit()
    await db.refresh(job)
    return await studio_queue.job_out(db, job)


@router.patch("/queue/{job_id}/priority")
async def patch_priority(
    job_id: uuid.UUID,
    body: PriorityPatch,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    if not _can_manage_queue(user):
        raise HTTPException(403, "Недостаточно прав для управления очередью")
    job = await db.get(StudioJob, job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    if job.status in studio_queue.FINAL_STATUSES:
        raise HTTPException(409, "Нельзя менять приоритет завершенной задачи")
    job.priority = body.priority
    await studio_queue.publish_queue_event(
        {
            "type": "job_updated",
            "job_id": str(job.id),
            "priority": job.priority,
            "owner_sub": job.owner_sub,
        }
    )
    await db.commit()
    await db.refresh(job)
    return await studio_queue.job_out(db, job)
