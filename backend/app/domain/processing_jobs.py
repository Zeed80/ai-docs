"""DocumentProcessingJob helpers, parameterised by a stage list.

Extracted verbatim from app/tasks/extraction.py (which owned the only pipeline
at the time) so that supplier-catalog ingestion can reuse the same job storage,
watchdog semantics and progress rendering instead of growing a parallel one.
The only behavioural addition is `set_step_progress`, for stages that know how
far along they are ("страница 40 из 200").
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy import update as _sa_update
from sqlalchemy.orm import Session

from app.db.models import DocumentProcessingJob
from app.domain.pipeline import PIPELINE_STEP_DEFINITIONS

StepDefs = Sequence[tuple[str, str]]


def default_pipeline_steps(defs: StepDefs = PIPELINE_STEP_DEFINITIONS) -> list[dict]:
    return [{"key": key, "label": label, "status": "pending"} for key, label in defs]


def latest_processing_job(db: Session, document_id) -> DocumentProcessingJob | None:
    return db.execute(
        select(DocumentProcessingJob)
        .where(DocumentProcessingJob.document_id == document_id)
        .order_by(DocumentProcessingJob.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def get_or_create_processing_job(
    db: Session,
    document_id,
    *,
    current_step: str,
    celery_task_id: str | None = None,
    defs: StepDefs = PIPELINE_STEP_DEFINITIONS,
    already_done: Sequence[str] = (),
) -> DocumentProcessingJob:
    job = latest_processing_job(db, document_id)
    if not job or job.status in {"done", "failed"}:
        steps = default_pipeline_steps(defs)
        for step in steps:
            if step["key"] in already_done:
                step["status"] = "done"
        job = DocumentProcessingJob(
            document_id=document_id,
            status="running",
            pipeline_steps=steps,
            current_step=current_step,
            started_at=datetime.now(UTC),
            celery_task_id=celery_task_id,
        )
        db.add(job)
        db.flush()
    else:
        job.status = "running"
        job.current_step = current_step
        job.started_at = job.started_at or datetime.now(UTC)
        if celery_task_id and not job.celery_task_id:
            job.celery_task_id = celery_task_id
    return job


def ensure_step_entries(
    job: DocumentProcessingJob, defs: StepDefs = PIPELINE_STEP_DEFINITIONS
) -> list[dict]:
    existing = {
        step.get("key"): dict(step)
        for step in (job.pipeline_steps or [])
        if isinstance(step, dict) and step.get("key")
    }
    steps = []
    for key, label in defs:
        step = existing.get(key, {"key": key, "label": label, "status": "pending"})
        step.setdefault("label", label)
        step.setdefault("status", "pending")
        steps.append(step)
    return steps


def set_job_step(
    job: DocumentProcessingJob,
    key: str,
    status: str,
    *,
    error: str | None = None,
    defs: StepDefs = PIPELINE_STEP_DEFINITIONS,
    **extra,
) -> None:
    steps = []
    for step in ensure_step_entries(job, defs):
        if step["key"] == key:
            step = {**step, "status": status}
            if error:
                step["error"] = error
            step.update(extra)
        steps.append(step)
    job.pipeline_steps = steps
    job.current_step = key if status in {"queued", "running", "failed"} else job.current_step
    if error:
        job.error = error


def set_step_progress(
    job: DocumentProcessingJob,
    key: str,
    done: int,
    total: int | None = None,
    *,
    defs: StepDefs = PIPELINE_STEP_DEFINITIONS,
) -> None:
    """Attach a counter to a running stage: {"progress": {"done": 40, "total": 200}}."""
    progress: dict = {"done": done}
    if total is not None:
        progress["total"] = total
    set_job_step(job, key, "running", defs=defs, progress=progress)


def step_status(
    job: DocumentProcessingJob, key: str, defs: StepDefs = PIPELINE_STEP_DEFINITIONS
) -> str | None:
    for step in ensure_step_entries(job, defs):
        if step["key"] == key:
            return step.get("status")
    return None


def skip_remaining_steps(
    job: DocumentProcessingJob,
    keys: set[str],
    defs: StepDefs = PIPELINE_STEP_DEFINITIONS,
) -> None:
    for key in keys:
        if step_status(job, key, defs) in {"pending", "queued", None}:
            set_job_step(job, key, "skipped", defs=defs)


def finish_job(job: DocumentProcessingJob, status: str, *, error: str | None = None) -> None:
    job.status = status
    job.error = error
    job.finished_at = datetime.now(UTC)
    if status == "done":
        job.current_step = "completed"


def touch_job(db: Session, job: DocumentProcessingJob) -> None:
    """Refresh job.updated_at so the watchdog doesn't fire during long inference."""
    db.execute(
        _sa_update(DocumentProcessingJob)
        .where(DocumentProcessingJob.id == job.id)
        .values(updated_at=datetime.now(UTC))
    )
    db.commit()
