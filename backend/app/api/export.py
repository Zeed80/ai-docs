"""Export API — Excel and 1C exports with approval gate for 1C."""

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import ExportJob, Invoice, ApprovalActionType, Approval, ApprovalStatus
from app.audit.service import log_action

router = APIRouter()
logger = structlog.get_logger()


class ExportJobOut(BaseModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    export_format: str
    status: str
    requested_by: str
    ready_at: datetime | None
    error: str | None

    model_config = {"from_attributes": True}


class ExportJobListResponse(BaseModel):
    items: list[ExportJobOut]
    total: int


# ── Invoice → Excel ────────────────────────────────────────────────────────


@router.post("/invoices/{invoice_id}/export", response_model=ExportJobOut, status_code=202)
async def export_invoice_excel(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Create Excel export for an invoice (no approval required)."""
    invoice = await db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    job = ExportJob(
        entity_type="invoice",
        entity_id=invoice_id,
        export_format="excel",
        status="pending",
        requested_by="user",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Queue Celery task
    try:
        from app.tasks.export import generate_excel_export
        generate_excel_export.delay(str(job.id))
    except Exception as e:
        logger.warning("export_task_queue_failed", error=str(e))

    await log_action(
        db, action="export.create_excel",
        entity_type="invoice", entity_id=invoice_id,
        details={"job_id": str(job.id)},
    )
    return job


# ── Invoice → 1C (approval gate) ──────────────────────────────────────────


@router.post("/invoices/{invoice_id}/export-1c", response_model=ExportJobOut, status_code=202)
async def export_invoice_1c(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Create 1C export for an invoice (requires approval)."""
    invoice = await db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    # Create approval gate
    approval = Approval(
        action_type=ApprovalActionType.invoice_approve,
        entity_type="invoice",
        entity_id=invoice_id,
        status=ApprovalStatus.pending,
        requested_by="user",
        context={"export_format": "1c_xml", "invoice_id": str(invoice_id)},
    )
    db.add(approval)
    await db.flush()

    job = ExportJob(
        entity_type="invoice",
        entity_id=invoice_id,
        export_format="1c_xml",
        status="pending",
        requested_by="user",
        approval_id=approval.id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    logger.info("export_1c_awaiting_approval", invoice_id=str(invoice_id), job_id=str(job.id))
    return job


# ── Export Jobs list ───────────────────────────────────────────────────────


@router.get("/export-jobs", response_model=ExportJobListResponse)
async def list_export_jobs(
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    status: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    q = select(ExportJob)
    if entity_type:
        q = q.where(ExportJob.entity_type == entity_type)
    if entity_id:
        q = q.where(ExportJob.entity_id == entity_id)
    if status:
        q = q.where(ExportJob.status == status)
    q = q.order_by(ExportJob.created_at.desc())

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (await db.execute(q.offset(offset).limit(limit))).scalars().all()
    return ExportJobListResponse(items=list(items), total=total)


@router.get("/export-jobs/{job_id}", response_model=ExportJobOut)
async def get_export_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(ExportJob, job_id)
    if not job:
        raise HTTPException(404, "Export job not found")
    return job


@router.get("/export-jobs/{job_id}/download")
async def download_export(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Redirect to presigned download URL for ready export."""
    job = await db.get(ExportJob, job_id)
    if not job:
        raise HTTPException(404, "Export job not found")
    if job.status != "ready":
        raise HTTPException(400, f"Export not ready (status: {job.status})")
    if not job.storage_path:
        raise HTTPException(500, "Export file path missing")

    try:
        from app.storage import get_presigned_url
        url = get_presigned_url(job.storage_path, expiry=3600)
        return RedirectResponse(url=url)
    except Exception as e:
        raise HTTPException(500, f"Could not generate download URL: {e}")
