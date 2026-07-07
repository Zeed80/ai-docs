"""Unified queue API for the graphic studio."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
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
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    q = select(StudioJob)
    if not _can_see_all(user):
        q = q.where(StudioJob.owner_sub == user.sub)
    if status:
        try:
            q = q.where(StudioJob.status == StudioJobStatus(status))
        except ValueError:
            raise HTTPException(400, f"Неизвестный статус очереди: {status}") from None
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
    await db.commit()
    await db.refresh(job)
    return await studio_queue.job_out(db, job)
