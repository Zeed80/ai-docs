"""Quarantine API — review and release or delete suspicious files."""

import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import (
    Document,
    DocumentStatus,
    FileExtensionAllowlist,
    QuarantineEntry,
)
from app.audit.service import log_action, add_timeline_event

router = APIRouter()
logger = structlog.get_logger()


class QuarantineEntryOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    reason: str
    original_filename: str
    detected_mime: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    decision: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class QuarantineListResponse(BaseModel):
    items: list[QuarantineEntryOut]
    total: int


class AllowlistEntryOut(BaseModel):
    id: uuid.UUID
    extension: str
    is_allowed: bool
    added_by: str

    model_config = {"from_attributes": True}


class AllowlistCreate(BaseModel):
    extension: str
    is_allowed: bool = True


# ── Quarantine list ────────────────────────────────────────────────────────


@router.get("", response_model=QuarantineListResponse)
async def list_quarantine(
    pending_only: bool = True,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: quarantine.list — List files waiting for quarantine review."""
    q = select(QuarantineEntry)
    if pending_only:
        q = q.where(QuarantineEntry.decision.is_(None))
    q = q.order_by(QuarantineEntry.created_at.desc())

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (await db.execute(q.offset(offset).limit(limit))).scalars().all()
    return QuarantineListResponse(items=list(items), total=total)


# ── Quarantine count (for sidebar badge) ──────────────────────────────────


@router.get("/count")
async def quarantine_count(db: AsyncSession = Depends(get_db)) -> dict:
    total = (await db.execute(
        select(func.count()).select_from(
            select(QuarantineEntry).where(QuarantineEntry.decision.is_(None)).subquery()
        )
    )).scalar() or 0
    return {"count": total}


# ── Release (allow processing) ─────────────────────────────────────────────


@router.post("/{entry_id}/release", response_model=QuarantineEntryOut)
async def release_quarantine(
    entry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Release file from quarantine and trigger extraction pipeline."""
    entry = await db.get(QuarantineEntry, entry_id)
    if not entry:
        raise HTTPException(404, "Quarantine entry not found")
    if entry.decision:
        raise HTTPException(400, f"Already decided: {entry.decision}")

    doc = await db.get(Document, entry.document_id)
    if not doc:
        raise HTTPException(404, "Document not found")

    entry.decision = "released"
    entry.reviewed_by = "user"
    entry.reviewed_at = datetime.now(timezone.utc)

    doc.status = DocumentStatus.ingested

    await log_action(
        db, action="quarantine.release",
        entity_type="document", entity_id=doc.id,
        details={"filename": entry.original_filename, "reason": entry.reason},
    )
    await add_timeline_event(
        db, entity_type="document", entity_id=doc.id,
        event_type="quarantine_released",
        summary=f"Файл освобождён из карантина: {entry.original_filename}",
        actor="user",
    )
    await db.commit()

    # Trigger extraction pipeline
    try:
        from app.tasks.extraction import process_document
        process_document.delay(str(doc.id))
    except Exception as e:
        logger.warning("extraction_queue_failed", doc_id=str(doc.id), error=str(e))

    await db.refresh(entry)
    return entry


# ── Delete quarantined file ────────────────────────────────────────────────


@router.delete("/{entry_id}", response_model=QuarantineEntryOut)
async def delete_quarantine(
    entry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete quarantined file from storage and mark as deleted."""
    entry = await db.get(QuarantineEntry, entry_id)
    if not entry:
        raise HTTPException(404, "Quarantine entry not found")
    if entry.decision:
        raise HTTPException(400, f"Already decided: {entry.decision}")

    doc = await db.get(Document, entry.document_id)

    # Try to delete from storage
    if doc:
        try:
            from app.storage import delete_file
            delete_file(doc.storage_path)
        except Exception as e:
            logger.warning("storage_delete_failed", error=str(e))

        doc.status = DocumentStatus.archived
        await log_action(
            db, action="quarantine.delete",
            entity_type="document", entity_id=doc.id,
            details={"filename": entry.original_filename},
        )

    entry.decision = "deleted"
    entry.reviewed_by = "user"
    entry.reviewed_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(entry)
    return entry


# ── Extension Allowlist ────────────────────────────────────────────────────


@router.get("/allowlist", response_model=list[AllowlistEntryOut])
async def get_allowlist(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(FileExtensionAllowlist).order_by(FileExtensionAllowlist.extension)
    )
    return result.scalars().all()


@router.post("/allowlist", response_model=AllowlistEntryOut, status_code=201)
async def add_to_allowlist(
    payload: AllowlistCreate,
    db: AsyncSession = Depends(get_db),
):
    ext = payload.extension.lower()
    if not ext.startswith("."):
        ext = "." + ext

    existing = await db.execute(
        select(FileExtensionAllowlist).where(FileExtensionAllowlist.extension == ext)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Extension {ext} already in allowlist")

    entry = FileExtensionAllowlist(extension=ext, is_allowed=payload.is_allowed, added_by="user")
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/allowlist/{entry_id}", status_code=204)
async def remove_from_allowlist(
    entry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    entry = await db.get(FileExtensionAllowlist, entry_id)
    if not entry:
        raise HTTPException(404, "Allowlist entry not found")
    await db.delete(entry)
    await db.commit()
