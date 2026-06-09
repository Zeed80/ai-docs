"""Document API — skills: doc.ingest, doc.get, doc.list, doc.update, doc.link"""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit.service import add_timeline_event, log_action
from app.auth.jwt import get_current_user, require_role
from app.auth.models import UserInfo, UserRole
from app.chat.store import append_chat_attachment
from app.domain.access import apply_visibility
from app.db.models import (
    Document,
    DocumentArtifact,
    DocumentChunk,
    DocumentExtraction,
    DocumentLink,
    DocumentProcessingJob,
    DocumentStatus,
    DocumentType,
    EvidenceSpan,
    FileExtensionAllowlist,
    GraphBuildStatus,
    GraphReviewItem,
    KnowledgeEdge,
    KnowledgeNode,
    MemoryEmbeddingRecord,
    NTDCheckFinding,
    NTDCheckRun,
    QuarantineEntry,
)
from app.config import settings
from app.db.session import get_db
from app.domain.document_deletion import (
    hard_delete_document,
    hard_delete_documents,
    purge_all_development_data,
)
from app.domain.documents import (
    DevelopmentPurgeRequest,
    DevelopmentPurgeResponse,
    DocumentBatchActionResponse,
    DocumentBatchActionResult,
    DocumentBatchRequest,
    DocumentBulkDeleteRequest,
    DocumentBulkDeleteResponse,
    DocumentDeleteResult,
    DocumentDependenciesResponse,
    DocumentIngestResponse,
    DocumentLinkCreate,
    DocumentLinkOut,
    DocumentLinkUpdate,
    DocumentListResponse,
    DocumentManagementSummary,
    DocumentOut,
    DocumentPipelineStatus,
    DocumentSummary,
    DocumentSummaryAI,
    DocumentUpdate,
    DocumentWorkspaceItem,
    DocumentWorkspaceResponse,
    FieldCorrectionRequest,
    FieldCorrectionResponse,
    TaskResponse,
)

router = APIRouter()
logger = structlog.get_logger()

# Unambiguous vector/CAD formats that auto-route to the drawing-analysis pipeline
# on ingest. Raster/PDF are deliberately EXCLUDED — they are ambiguous (usually
# invoices/letters) and must be classified first; classify_document creates a
# drawing only when it detects doc_type=drawing. (Auto-routing every raster/PDF
# here funnelled all uploaded invoices into heavy VLM drawing analysis, which
# monopolised the single-flight GPU lane and stalled OCR.)
DRAWING_AUTO_ROUTE_EXTENSIONS = {"dwg", "dxf", "step", "stp", "iges", "igs", "svg"}

# ── Fast type detection (no AI, instant) ─────────────────────────────────────

_EXT_TO_DOC_TYPE: dict[str, str] = {
    # Technical drawings (unambiguous)
    ".dwg": "drawing",
    ".dxf": "drawing",
    ".svg": "drawing",
    ".step": "drawing",
    ".stp": "drawing",
    ".iges": "drawing",
    ".igs": "drawing",
    # Spreadsheet-based financials
    ".xlsx": "invoice",
    ".xls": "invoice",
    # Office documents
    ".docx": "letter",
    ".doc": "letter",
    ".odt": "letter",
    # Email
    ".eml": "letter",
    ".msg": "letter",
}

_MIME_TO_DOC_TYPE: dict[str, str] = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "invoice",
    "application/vnd.ms-excel": "invoice",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "letter",
    "application/msword": "letter",
    "message/rfc822": "letter",
}


def _quick_detect_doc_type(
    filename: str, mime_type: str
) -> tuple[str | None, str | None]:
    """
    Return (doc_type, source) based on file extension and MIME type alone.
    No AI call — instantaneous.

    Returns (None, None) when detection is ambiguous (e.g. PDF, images, CSV).
    """
    ext = Path(filename).suffix.lower()
    if ext in _EXT_TO_DOC_TYPE:
        return _EXT_TO_DOC_TYPE[ext], "extension"
    if mime_type in _MIME_TO_DOC_TYPE:
        return _MIME_TO_DOC_TYPE[mime_type], "mime"
    return None, None


PIPELINE_STEP_DEFINITIONS = [
    ("store", "Файл сохранен"),
    ("memory_seed", "Первичная память"),
    ("classification", "Классификация"),
    ("extraction", "Распознавание"),
    ("sql_records", "Записи SQL"),
    ("memory_graph", "Память и граф"),
    ("embedding", "Векторизация"),
]

DEFAULT_ALLOWED_EXTENSIONS = {
    ".bmp",
    ".csv",
    ".doc",
    ".docx",
    ".dwg",
    ".dxf",
    ".eml",
    ".gif",
    ".iges",
    ".igs",
    ".jpeg",
    ".jpg",
    ".json",
    ".log",
    ".md",
    ".msg",
    ".odt",
    ".pdf",
    ".png",
    ".step",
    ".stp",
    ".svg",
    ".tif",
    ".tiff",
    ".txt",
    ".webp",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".xml",
}


def _delete_result_payload(result: dict) -> DocumentDeleteResult:
    return DocumentDeleteResult(
        document_id=uuid.UUID(str(result["document_id"])),
        deleted=int(result.get("deleted") or 0),
        missing=int(result.get("missing") or 0),
        storage_deleted=int(result.get("storage_deleted") or 0),
        details={k: v for k, v in result.items() if k not in {"document_id"}},
    )


async def _count_for(db: AsyncSession, entity, *conditions) -> int:
    query = select(func.count()).select_from(entity)
    if conditions:
        query = query.where(*conditions)
    return (await db.execute(query)).scalar() or 0


async def _document_text_for_memory(db: AsyncSession, doc: Document) -> str:
    chunks = (
        await db.execute(
            select(DocumentChunk.text)
            .where(DocumentChunk.document_id == doc.id)
            .order_by(DocumentChunk.chunk_index.asc())
            .limit(200)
        )
    ).scalars().all()
    if chunks:
        return "\n\n".join(chunks)

    extraction = (
        await db.execute(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id == doc.id)
            .order_by(DocumentExtraction.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if extraction and extraction.raw_output:
        return str(extraction.raw_output)

    return doc.file_name


async def _pipeline_status_for_document(
    db: AsyncSession,
    document_id: uuid.UUID,
) -> DocumentPipelineStatus:
    latest_job = (
        await db.execute(
            select(DocumentProcessingJob)
            .where(DocumentProcessingJob.document_id == document_id)
            .order_by(DocumentProcessingJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    latest_graph = (
        await db.execute(
            select(GraphBuildStatus)
            .where(GraphBuildStatus.document_id == document_id)
            .order_by(GraphBuildStatus.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return DocumentPipelineStatus(
        processing_status=latest_job.status if latest_job else None,
        current_step=latest_job.current_step if latest_job else None,
        processing_error=latest_job.error if latest_job else None,
        pipeline_steps=latest_job.pipeline_steps if latest_job else [],
        extraction_count=await _count_for(
            db,
            DocumentExtraction,
            DocumentExtraction.document_id == document_id,
        ),
        artifact_count=await _count_for(
            db,
            DocumentArtifact,
            DocumentArtifact.document_id == document_id,
        ),
        graph_status=latest_graph.status if latest_graph else None,
        graph_scope=latest_graph.build_scope if latest_graph else None,
        graph_error=latest_graph.error if latest_graph else None,
        memory_chunks=await _count_for(db, DocumentChunk, DocumentChunk.document_id == document_id),
        evidence_spans=await _count_for(db, EvidenceSpan, EvidenceSpan.document_id == document_id),
        graph_nodes=await _count_for(
            db,
            KnowledgeNode,
            KnowledgeNode.source_document_id == document_id,
        ),
        graph_edges=await _count_for(
            db,
            KnowledgeEdge,
            KnowledgeEdge.source_document_id == document_id,
        ),
        graph_review_pending=await _count_for(
            db,
            GraphReviewItem,
            GraphReviewItem.document_id == document_id,
            GraphReviewItem.status == "pending",
        ),
        embedding_records=await _count_for(
            db,
            MemoryEmbeddingRecord,
            MemoryEmbeddingRecord.document_id == document_id,
        ),
        ntd_checks=await _count_for(db, NTDCheckRun, NTDCheckRun.document_id == document_id),
        ntd_open_findings=await _count_for(
            db,
            NTDCheckFinding,
            NTDCheckFinding.document_id == document_id,
            NTDCheckFinding.status == "open",
        ),
    )


def _initial_pipeline_steps(*, memory_seed_done: bool = False) -> list[dict]:
    steps = []
    for key, label in PIPELINE_STEP_DEFINITIONS:
        status = "pending"
        if key == "store":
            status = "done"
        elif key == "memory_seed" and memory_seed_done:
            status = "done"
        steps.append({"key": key, "label": label, "status": status})
    return steps


def _mark_pipeline_step(steps: list[dict], key: str, status: str) -> list[dict]:
    return [
        {**step, "status": status} if step.get("key") == key else step
        for step in steps
    ]


async def _create_processing_job(
    db: AsyncSession,
    doc: Document,
    *,
    status: str,
    current_step: str | None,
    memory_seed_done: bool = False,
) -> DocumentProcessingJob:
    steps = _initial_pipeline_steps(memory_seed_done=memory_seed_done)
    if current_step:
        step_status = status if status == "queued" else "running"
        steps = _mark_pipeline_step(steps, current_step, step_status)
    job = DocumentProcessingJob(
        document_id=doc.id,
        status=status,
        pipeline_steps=steps,
        current_step=current_step,
        started_at=datetime.now(UTC) if status == "running" else None,
    )
    db.add(job)
    await db.flush()
    return job


async def _ingest_eml_attachments(
    db: AsyncSession,
    parent_doc: Document,
    content: bytes,
    *,
    owner_sub: str | None,
    department_id: uuid.UUID | None,
) -> list[Document]:
    """Best-effort: store each .eml attachment as a linked, auto-processed Document.

    Attachments are extracted by the shared parser registry (same MIME parser as
    IMAP polling). Each allowed attachment becomes a child Document linked to the
    parent email via a ``email_attachment`` :class:`DocumentLink`, deduplicated by
    SHA-256. Never raises — failures are logged and skipped so the parent ingest
    always succeeds.
    """
    from app.ai.parsers import parse_document
    from app.storage import file_exists, upload_file

    parsed = parse_document(content, parent_doc.file_name, parent_doc.mime_type)
    attachments = parsed.meta.get("attachments") or []
    new_children: list[Document] = []
    for att in attachments:
        try:
            att_bytes = att.get("content") or b""
            att_name = att.get("filename") or "attachment"
            if not att_bytes:
                continue
            ext = Path(att_name).suffix.lower()
            if ext not in DEFAULT_ALLOWED_EXTENSIONS:
                logger.info("eml_attachment_skipped_ext", filename=att_name, ext=ext)
                continue
            att_hash = att.get("sha256") or hashlib.sha256(att_bytes).hexdigest()
            existing = await db.execute(
                select(Document).where(Document.file_hash == att_hash)
            )
            if existing.scalar_one_or_none() is not None:
                continue  # already ingested elsewhere; skip duplicate

            storage_path = f"documents/{att_hash[:2]}/{att_hash[2:4]}/{att_hash}"
            if not file_exists(storage_path):
                upload_file(att_bytes, storage_path, att.get("content_type"))
            dt, src = _quick_detect_doc_type(att_name, att.get("content_type") or "")
            child = Document(
                file_name=att_name,
                file_hash=att_hash,
                file_size=len(att_bytes),
                mime_type=att.get("content_type") or "application/octet-stream",
                storage_path=storage_path,
                source_channel="email",
                status="ingested",
                doc_type=dt,
                doc_type_confidence=0.9 if dt else None,
                owner_sub=owner_sub,
                department_id=department_id,
                metadata_={"doc_type_source": src} if src else None,
            )
            db.add(child)
            await db.flush()
            await _create_processing_job(
                db, child, status="queued", current_step="classification"
            )
            db.add(
                DocumentLink(
                    document_id=child.id,
                    linked_entity_type="document",
                    linked_entity_id=parent_doc.id,
                    link_type="email_attachment",
                )
            )
            new_children.append(child)
        except Exception as exc:
            logger.warning("eml_attachment_ingest_failed", error=str(exc))
    return new_children


# ── doc.download + presigned URL ─────────────────────────────────────────────


@router.get("/{document_id}/download")
async def download_document(
    document_id: uuid.UUID,
    inline: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Download original file from MinIO. inline=true for browser display."""
    from fastapi.responses import Response

    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        from app.storage import download_file
        content = download_file(doc.storage_path)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Storage unavailable: {e}")

    safe_name = quote(doc.file_name)
    disposition = "inline" if inline else f"attachment; filename*=UTF-8''{safe_name}"
    return Response(
        content=content,
        media_type=doc.mime_type,
        headers={"Content-Disposition": disposition},
    )


@router.get("/{document_id}/presigned-url")
async def get_document_presigned_url(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get a presigned URL for direct browser access to the file."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        from app.storage import get_presigned_url
        url = get_presigned_url(doc.storage_path)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Storage unavailable: {e}")

    return {"url": url, "file_name": doc.file_name, "mime_type": doc.mime_type}


# ── doc.ingest ───────────────────────────────────────────────────────────────


@router.post("/ingest", response_model=DocumentIngestResponse)
async def ingest_document(
    file: UploadFile = File(...),
    source_channel: str = Query("upload"),
    chat_session_id: uuid.UUID | None = Query(None),
    requested_doc_type: DocumentType | None = Query(None),
    auto_process: bool = Query(True),
    auto_verify: bool = Query(False),
    manual_doc_type_override: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.ingest — Accept file, store, create Document record."""
    content = await file.read()
    file_size = len(content)

    # File size limit check (before antivirus — saves resources on huge files)
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if file_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "stage": "validation",
                "file_name": file.filename or "unknown",
                "error": f"File too large: {file_size // (1024*1024)} MB exceeds limit of {settings.max_upload_size_mb} MB",
            },
        )

    if file_size == 0:
        raise HTTPException(
            status_code=422,
            detail={
                "stage": "validation",
                "file_name": file.filename or "unknown",
                "error": "Empty file is not allowed",
            },
        )

    file_hash = hashlib.sha256(content).hexdigest()

    # ClamAV antivirus scan (skipped gracefully if clamd not available)
    from app.security.clamav import scan_bytes
    scan = scan_bytes(content)
    if not scan.is_clean:
        raise HTTPException(
            status_code=422,
            detail=f"File rejected by antivirus scanner: {scan.threat}",
        )
    if not scan.skipped:
        logger.info("clamav_scan_clean", file_hash=file_hash)

    # SHA-256 dedup check
    existing = await db.execute(select(Document).where(Document.file_hash == file_hash))
    duplicate = existing.scalar_one_or_none()
    if duplicate:
        logger.info("duplicate_detected", file_hash=file_hash, original_id=str(duplicate.id))
        return DocumentIngestResponse(
            id=duplicate.id,
            file_name=duplicate.file_name,
            file_hash=duplicate.file_hash,
            file_size=duplicate.file_size,
            mime_type=duplicate.mime_type,
            status=duplicate.status,
            is_duplicate=True,
            duplicate_of=duplicate.id,
            pipeline_queued=False,
            created_at=duplicate.created_at,
        )

    # Extension allowlist check
    ext = Path(file.filename or "").suffix.lower()
    allowed_result = await db.execute(
        select(FileExtensionAllowlist).where(
            FileExtensionAllowlist.extension == ext,
            FileExtensionAllowlist.is_allowed,
        )
    )
    is_allowed = (
        allowed_result.scalar_one_or_none() is not None
        or ext in DEFAULT_ALLOWED_EXTENSIONS
    )

    # Store to MinIO (even suspicious files — reviewer may release them)
    storage_path = f"documents/{file_hash[:2]}/{file_hash[2:4]}/{file_hash}"
    try:
        from app.storage import upload_file
        upload_file(content, storage_path, file.content_type or "application/octet-stream")
    except Exception as e:
        logger.warning("minio_upload_failed", error=str(e), path=storage_path)

    # Fast type detection: extension → MIME → manual override (in that priority order)
    mime_type_str = file.content_type or "application/octet-stream"
    fast_type, fast_source = _quick_detect_doc_type(file.filename or "", mime_type_str)

    # Determine the effective doc_type to persist immediately:
    # 1. Manual override (user explicitly chose) → highest priority
    # 2. Requested type without override (user pre-filled from extension) → use it
    # 3. Fast detection from extension → set without override flag
    # 4. None → AI will classify later
    effective_doc_type: str | None = None
    effective_confidence: float | None = None
    effective_source: str | None = None

    if requested_doc_type and manual_doc_type_override:
        effective_doc_type = requested_doc_type
        effective_confidence = 1.0
        effective_source = "manual"
    elif requested_doc_type:
        effective_doc_type = requested_doc_type
        effective_confidence = 0.85
        effective_source = "suggested"
    elif fast_type:
        effective_doc_type = fast_type
        effective_confidence = 0.9
        effective_source = fast_source

    initial_status = DocumentStatus.ingested if is_allowed else DocumentStatus.suspicious
    initial_metadata: dict | None = None
    if (requested_doc_type and manual_doc_type_override) or auto_verify:
        initial_metadata = {}
        if requested_doc_type and manual_doc_type_override:
            initial_metadata["manual_doc_type_override"] = manual_doc_type_override
        if auto_verify:
            initial_metadata["auto_verify"] = True
    if effective_source and not initial_metadata:
        initial_metadata = {}
    if effective_source and initial_metadata is not None:
        initial_metadata["doc_type_source"] = effective_source

    doc = Document(
        file_name=file.filename or "unknown",
        file_hash=file_hash,
        file_size=file_size,
        mime_type=mime_type_str,
        storage_path=storage_path,
        source_channel=source_channel,
        status=initial_status,
        doc_type=effective_doc_type,
        doc_type_confidence=effective_confidence,
        metadata_=initial_metadata,
    )
    db.add(doc)
    await db.flush()

    if not is_allowed:
        quarantine_entry = QuarantineEntry(
            document_id=doc.id,
            reason="extension_not_allowed",
            original_filename=file.filename or "unknown",
            detected_mime=file.content_type,
        )
        db.add(quarantine_entry)
        await log_action(
            db,
            action="doc.quarantine",
            entity_type="document",
            entity_id=doc.id,
            details={
                "filename": doc.file_name,
                "extension": ext,
                "reason": "extension_not_allowed",
            },
        )
        await add_timeline_event(
            db, entity_type="document", entity_id=doc.id,
            event_type="quarantined",
            summary=f"Файл помещён в карантин: {doc.file_name} (расширение {ext} не разрешено)",
            actor="system",
        )
        await db.commit()
        logger.info("document_quarantined", doc_id=str(doc.id), ext=ext)
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=202,
            content={
                "quarantined": True,
                "document_id": str(doc.id),
                "reason": "extension_not_allowed",
                "extension": ext,
            },
        )

    await log_action(db, action="doc.ingest", entity_type="document", entity_id=doc.id)
    if source_channel == "chat" and chat_session_id is not None:
        await append_chat_attachment(
            db,
            session_id=chat_session_id,
            message_id=None,
            document_id=doc.id,
            file_name=doc.file_name,
            mime_type=doc.mime_type,
            size_bytes=doc.file_size,
            metadata={"source": "ingest"},
        )
    await add_timeline_event(
        db,
        entity_type="document",
        entity_id=doc.id,
        event_type="ingested",
        summary=f"Document ingested: {doc.file_name}",
        actor="system",
    )

    memory_seed_done = False
    try:
        from app.domain.memory_builder import build_document_memory_async

        memory_result = await build_document_memory_async(db, doc, text=doc.file_name)
        memory_seed_done = True
        await add_timeline_event(
            db,
            entity_type="document",
            entity_id=doc.id,
            event_type="memory_indexed",
            summary="Document added to graph memory",
            actor="system",
            details=memory_result.__dict__,
        )
    except Exception as e:
        logger.warning("memory_index_failed", doc_id=str(doc.id), error=str(e))

    pipeline_queued = False
    processing_job: DocumentProcessingJob | None = None
    if auto_process:
        processing_job = await _create_processing_job(
            db,
            doc,
            status="queued",
            current_step="classification",
            memory_seed_done=memory_seed_done,
        )
    else:
        processing_job = await _create_processing_job(
            db,
            doc,
            status="done",
            current_step=None,
            memory_seed_done=memory_seed_done,
        )
        processing_job.finished_at = datetime.now(UTC)

    await db.commit()

    logger.info("document_ingested", doc_id=str(doc.id), file_name=doc.file_name)

    file_ext_lower = Path(doc.file_name or "").suffix.lower()

    # .eml / .msg → split out embedded attachments as linked child documents
    if file_ext_lower in {".eml", ".msg"}:
        try:
            children = await _ingest_eml_attachments(
                db,
                doc,
                content,
                owner_sub=doc.owner_sub,
                department_id=doc.department_id,
            )
            if children:
                await db.commit()
                from app.tasks.extraction import process_document as _process_doc

                for child in children:
                    try:
                        _process_doc.delay(str(child.id))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "eml_attachment_queue_failed",
                            doc_id=str(child.id),
                            error=str(exc),
                        )
                logger.info(
                    "eml_attachments_ingested",
                    doc_id=str(doc.id),
                    count=len(children),
                )
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            logger.warning("eml_attachment_split_failed", doc_id=str(doc.id), error=str(exc))

    # Only unambiguous vector/CAD formats auto-route to drawing analysis (see
    # DRAWING_AUTO_ROUTE_EXTENSIONS). Raster/PDF go through normal classification.
    fmt = file_ext_lower.lstrip(".")
    if fmt in DRAWING_AUTO_ROUTE_EXTENSIONS:
        try:
            from app.services.drawing_service import create_and_analyze_drawing
            from app.storage import download_file
            file_bytes = download_file(doc.storage_path) if doc.storage_path else b""
            drawing, drawing_task_id = await create_and_analyze_drawing(
                file_bytes=file_bytes,
                filename=doc.file_name or "",
                fmt=fmt,
                db=db,
                document_id=doc.id,
                is_confidential=True,
                allow_cloud=False,
                max_views=6,
                created_by="document_ingest",
            )
            logger.info(
                "drawing_analysis_auto_queued",
                doc_id=str(doc.id),
                drawing_id=str(drawing.id),
                task_id=drawing_task_id,
            )
        except Exception as e:
            logger.warning("drawing_auto_queue_failed", doc_id=str(doc.id), error=str(e))

    if auto_process:
        # Auto-trigger extraction pipeline
        try:
            from app.tasks.extraction import process_document
            task = process_document.delay(str(doc.id))
            if processing_job:
                processing_job.celery_task_id = str(task.id) if task else None
                await db.commit()
            pipeline_queued = True
            logger.info("extraction_queued", doc_id=str(doc.id))
        except Exception as e:
            if processing_job:
                processing_job.status = "failed"
                processing_job.error = str(e)
                processing_job.pipeline_steps = _mark_pipeline_step(
                    processing_job.pipeline_steps,
                    "classification",
                    "failed",
                )
                processing_job.finished_at = datetime.now(UTC)
                processing_job.current_step = "classification"
                await db.commit()
            logger.warning("extraction_queue_failed", doc_id=str(doc.id), error=str(e))

    # Embedding is triggered at the END of extract_invoice (after OCR completes) to avoid
    # loading both the OCR model and the embedding model simultaneously.
    # Only schedule here when the extraction pipeline is NOT running.
    if not auto_process:
        try:
            from app.tasks.embedding import embed_document
            embed_document.delay(str(doc.id))
        except Exception as e:
            logger.warning("embed_queue_failed", doc_id=str(doc.id), error=str(e))

    return DocumentIngestResponse(
        id=doc.id,
        file_name=doc.file_name,
        file_hash=doc.file_hash,
        file_size=doc.file_size,
        mime_type=doc.mime_type,
        status=doc.status,
        pipeline_queued=pipeline_queued,
        created_at=doc.created_at,
        detected_type=effective_doc_type,
        detected_type_source=effective_source,
    )


# ── doc.list / doc.workspace ─────────────────────────────────────────────────


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    status: DocumentStatus | None = None,
    doc_type: DocumentType | None = None,
    source_channel: str | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """Skill: doc.list — List documents with filters."""
    query = select(Document)

    if status:
        query = query.where(Document.status == status)
    if doc_type:
        query = query.where(Document.doc_type == doc_type)
    if source_channel:
        query = query.where(Document.source_channel == source_channel)
    if search:
        query = query.where(Document.file_name.ilike(f"%{search}%"))

    # Row-level visibility: hide other departments' owned documents from non-managers.
    query = await apply_visibility(
        db, current_user, query,
        owner_col=Document.owner_sub, department_col=Document.department_id,
    )

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Fetch
    query = query.order_by(Document.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    items = result.scalars().all()

    return DocumentListResponse(items=items, total=total, offset=offset, limit=limit)


@router.get("/workspace", response_model=DocumentWorkspaceResponse)
async def list_document_workspace(
    status: DocumentStatus | None = None,
    doc_type: DocumentType | None = None,
    source_channel: str | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.workspace — List documents with compact pipeline summaries."""
    query = select(Document)

    if status:
        query = query.where(Document.status == status)
    if doc_type:
        query = query.where(Document.doc_type == doc_type)
    if source_channel:
        query = query.where(Document.source_channel == source_channel)
    if search:
        query = query.where(
            or_(
                Document.file_name.ilike(f"%{search}%"),
                Document.file_hash.ilike(f"%{search}%"),
            )
        )

    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar() or 0
    result = await db.execute(
        query.order_by(Document.created_at.desc()).offset(offset).limit(limit)
    )
    docs = result.scalars().all()

    status_counts_result = await db.execute(
        select(Document.status, func.count()).group_by(Document.status)
    )
    status_counts = {
        status.value if hasattr(status, "value") else str(status): int(count)
        for status, count in status_counts_result.all()
    }
    type_counts_result = await db.execute(
        select(Document.doc_type, func.count())
        .where(Document.doc_type.is_not(None))
        .group_by(Document.doc_type)
    )
    doc_type_counts = {
        doc_type.value if hasattr(doc_type, "value") else str(doc_type): int(count)
        for doc_type, count in type_counts_result.all()
    }

    items = [
        DocumentWorkspaceItem(
            document=doc,
            pipeline=await _pipeline_status_for_document(db, doc.id),
        )
        for doc in docs
    ]
    return DocumentWorkspaceResponse(
        items=items,
        total=total,
        offset=offset,
        limit=limit,
        status_counts=status_counts,
        doc_type_counts=doc_type_counts,
    )


# ── doc.invoice ──────────────────────────────────────────────────────────────


@router.get("/{document_id}/invoice")
async def get_document_invoice(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return invoice with line items for a document."""
    from app.db.models import Invoice, InvoiceLine  # noqa: F401
    from sqlalchemy.orm import selectinload as _sil

    # Try direct FK first (invoice.document_id), then via DocumentLink
    inv_result = await db.execute(
        select(Invoice)
        .where(Invoice.document_id == document_id)
        .options(_sil(Invoice.lines))
    )
    invoice = inv_result.scalar_one_or_none()

    if not invoice:
        link_result = await db.execute(
            select(DocumentLink).where(
                DocumentLink.document_id == document_id,
                DocumentLink.linked_entity_type == "invoice",
            )
        )
        link = link_result.scalar_one_or_none()
        if link:
            inv_result2 = await db.execute(
                select(Invoice)
                .where(Invoice.id == link.linked_entity_id)
                .options(_sil(Invoice.lines))
            )
            invoice = inv_result2.scalar_one_or_none()

    if invoice:
        return {
            "id": str(invoice.id),
            "preview": False,
            "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date.date().isoformat() if invoice.invoice_date else None,
            "due_date": invoice.due_date.date().isoformat() if invoice.due_date else None,
            "currency": invoice.currency,
            "subtotal": float(invoice.subtotal) if invoice.subtotal is not None else None,
            "tax_amount": float(invoice.tax_amount) if invoice.tax_amount is not None else None,
            "total_amount": float(invoice.total_amount) if invoice.total_amount is not None else None,
            "status": invoice.status.value if invoice.status else None,
            "lines": [
                {
                    "id": str(l.id),
                    "line_number": l.line_number,
                    "sku": l.sku,
                    "description": l.description,
                    "quantity": float(l.quantity) if l.quantity is not None else None,
                    "unit": l.unit,
                    "unit_price": float(l.unit_price) if l.unit_price is not None else None,
                    "amount": float(l.amount) if l.amount is not None else None,
                    "tax_rate": float(l.tax_rate) if l.tax_rate is not None else None,
                    "tax_amount": float(l.tax_amount) if l.tax_amount is not None else None,
                    "confidence": float(l.confidence) if l.confidence is not None else None,
                }
                for l in sorted(invoice.lines, key=lambda x: x.line_number)
            ],
        }

    # No confirmed Invoice yet — return preview from extraction structured_data
    ext_result = await db.execute(
        select(DocumentExtraction)
        .where(DocumentExtraction.document_id == document_id)
        .order_by(DocumentExtraction.created_at.desc())
        .limit(1)
    )
    extraction = ext_result.scalar_one_or_none()
    if not extraction or not extraction.structured_data:
        raise HTTPException(status_code=404, detail="No invoice found for this document")

    data = extraction.structured_data
    lines_raw = data.get("lines") or []
    return {
        "id": None,
        "preview": True,
        "invoice_number": data.get("invoice_number"),
        "invoice_date": data.get("invoice_date"),
        "due_date": data.get("due_date"),
        "currency": data.get("currency"),
        "subtotal": data.get("subtotal"),
        "tax_amount": data.get("tax_amount"),
        "total_amount": data.get("total_amount"),
        "status": "preview",
        "lines": [
            {
                "id": None,
                "line_number": l.get("line_number", i + 1),
                "sku": l.get("sku"),
                "description": l.get("description"),
                "quantity": l.get("quantity"),
                "unit": l.get("unit"),
                "unit_price": l.get("unit_price"),
                "amount": l.get("amount"),
                "tax_rate": l.get("tax_rate"),
                "tax_amount": l.get("tax_amount"),
                "confidence": None,
            }
            for i, l in enumerate(lines_raw)
        ],
    }


# ── doc.get ──────────────────────────────────────────────────────────────────


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.get — Get document with extractions and links."""
    result = await db.execute(
        select(Document)
        .where(Document.id == document_id)
        .options(
            selectinload(Document.extractions).selectinload(DocumentExtraction.fields),
            selectinload(Document.links),
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.delete("/bulk-delete", response_model=DocumentBulkDeleteResponse)
async def bulk_delete_documents(
    payload: DocumentBulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_role(UserRole.manager)),
):
    """Skill: doc.bulk_delete — Hard-delete selected documents and derived records."""
    result = await hard_delete_documents(
        db,
        payload.document_ids,
        delete_files=payload.delete_files,
    )
    await db.commit()
    return DocumentBulkDeleteResponse(
        deleted=int(result["deleted"]),
        missing=int(result["missing"]),
        results=[_delete_result_payload(item) for item in result["results"]],
    )


@router.post("/batch/process", response_model=DocumentBatchActionResponse)
async def batch_process_documents(
    payload: DocumentBatchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.batch_process — Trigger full processing for selected documents."""
    from app.tasks.extraction import process_document

    results: list[DocumentBatchActionResult] = []
    for document_id in payload.document_ids:
        doc = await db.get(Document, document_id)
        if not doc:
            results.append(DocumentBatchActionResult(document_id=document_id, status="missing"))
            continue
        if doc.status == DocumentStatus.suspicious:
            results.append(
                DocumentBatchActionResult(
                    document_id=document_id,
                    status="skipped",
                    detail="quarantined",
                )
            )
            continue
        job = await _create_processing_job(
            db,
            doc,
            status="queued",
            current_step="classification",
            memory_seed_done=True,
        )
        task = process_document.delay(str(document_id), payload.force)
        job.celery_task_id = str(task.id) if task else None
        await db.commit()
        results.append(
            DocumentBatchActionResult(
                document_id=document_id,
                status="queued",
                task_id=task.id,
            )
        )
    return DocumentBatchActionResponse(action="process", results=results)


@router.post("/batch/classify", response_model=DocumentBatchActionResponse)
async def batch_classify_documents(
    payload: DocumentBatchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.batch_classify — Trigger classification for selected documents."""
    from app.tasks.extraction import classify_document as classify_task

    results: list[DocumentBatchActionResult] = []
    for document_id in payload.document_ids:
        doc = await db.get(Document, document_id)
        if not doc:
            results.append(DocumentBatchActionResult(document_id=document_id, status="missing"))
            continue
        job = await _create_processing_job(
            db,
            doc,
            status="queued",
            current_step="classification",
            memory_seed_done=True,
        )
        task = classify_task.delay(str(document_id), payload.force)
        job.celery_task_id = str(task.id) if task else None
        await db.commit()
        results.append(
            DocumentBatchActionResult(
                document_id=document_id,
                status="queued",
                task_id=task.id,
            )
        )
    return DocumentBatchActionResponse(action="classify", results=results)


@router.post("/batch/embeddings-reindex", response_model=DocumentBatchActionResponse)
async def batch_reindex_document_embeddings(
    payload: DocumentBatchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.batch_embeddings_reindex — Queue embedding rebuild for selected documents."""
    from app.tasks.embedding import embed_document

    results: list[DocumentBatchActionResult] = []
    for document_id in payload.document_ids:
        doc = await db.get(Document, document_id)
        if not doc:
            results.append(DocumentBatchActionResult(document_id=document_id, status="missing"))
            continue
        task = embed_document.delay(str(document_id))
        results.append(
            DocumentBatchActionResult(
                document_id=document_id,
                status="queued",
                task_id=task.id,
            )
        )
    return DocumentBatchActionResponse(action="embeddings-reindex", results=results)


@router.post("/batch/memory-rebuild", response_model=DocumentBatchActionResponse)
async def batch_rebuild_document_memory(
    payload: DocumentBatchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.batch_memory_rebuild — Rebuild graph memory for selected documents."""
    from app.domain.memory_builder import build_document_memory_async

    results: list[DocumentBatchActionResult] = []
    for document_id in payload.document_ids:
        doc = await db.get(Document, document_id)
        if not doc:
            results.append(DocumentBatchActionResult(document_id=document_id, status="missing"))
            continue
        try:
            text = await _document_text_for_memory(db, doc)
            await build_document_memory_async(
                db,
                doc,
                text=text,
                build_scope=payload.build_scope,
                actor="user",
                clear_existing=True,
            )
            await db.commit()
            results.append(DocumentBatchActionResult(document_id=document_id, status="completed"))
        except Exception as exc:
            await db.rollback()
            results.append(
                DocumentBatchActionResult(
                    document_id=document_id,
                    status="failed",
                    detail=str(exc),
                )
            )
    return DocumentBatchActionResponse(action="memory-rebuild", results=results)


@router.post("/batch/ntd-check", response_model=DocumentBatchActionResponse)
async def batch_run_ntd_checks(
    payload: DocumentBatchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.batch_ntd_check — Run manual NTD checks for selected documents."""
    from app.api.ntd import _run_ntd_check
    from app.domain.ntd import NTDCheckRunRequest

    results: list[DocumentBatchActionResult] = []
    for document_id in payload.document_ids:
        doc = await db.get(Document, document_id)
        if not doc:
            results.append(DocumentBatchActionResult(document_id=document_id, status="missing"))
            continue
        try:
            await _run_ntd_check(
                db,
                NTDCheckRunRequest(
                    document_id=document_id,
                    triggered_by="manual",
                    actor="user",
                ),
            )
            results.append(DocumentBatchActionResult(document_id=document_id, status="completed"))
        except Exception as exc:
            await db.rollback()
            results.append(
                DocumentBatchActionResult(
                    document_id=document_id,
                    status="failed",
                    detail=str(exc),
                )
            )
    return DocumentBatchActionResponse(action="ntd-check", results=results)


@router.post("/dev/purge-all", response_model=DevelopmentPurgeResponse)
async def purge_all_documents_for_development(
    payload: DevelopmentPurgeRequest,
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_role(UserRole.admin)),
):
    """Dev-only hard purge of documents and all derived DB records."""
    if settings.app_env == "production":
        raise HTTPException(status_code=403, detail="This endpoint is disabled in production")
    if payload.confirm != "DELETE ALL DOCUMENT DATA":
        raise HTTPException(
            status_code=400,
            detail='Confirmation must equal "DELETE ALL DOCUMENT DATA"',
        )

    result = await purge_all_development_data(db, delete_files=payload.delete_files)
    await db.commit()
    return DevelopmentPurgeResponse(
        deleted=int(result["deleted"]),
        missing=int(result["missing"]),
        documents_seen=int(result["documents_seen"]),
        results=[_delete_result_payload(item) for item in result["results"]],
    )


# ── doc.management ──────────────────────────────────────────────────────────


@router.get("/{document_id}/management", response_model=DocumentManagementSummary)
async def get_document_management_summary(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.management — Read document pipeline, memory, graph and NTD status."""
    result = await db.execute(
        select(Document)
        .where(Document.id == document_id)
        .options(selectinload(Document.links))
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    pipeline = await _pipeline_status_for_document(db, document_id)
    return DocumentManagementSummary(document=doc, pipeline=pipeline, links=doc.links)


# ── doc.update ───────────────────────────────────────────────────────────────


@router.patch("/{document_id}", response_model=DocumentSummary)
async def update_document(
    document_id: uuid.UUID,
    payload: DocumentUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.update — Update document fields."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    update_data = payload.model_dump(exclude_unset=True)
    manual_override = update_data.pop("manual_doc_type_override", None)
    for field, value in update_data.items():
        if field == "metadata_":
            doc.metadata_ = value
        else:
            setattr(doc, field, value)

    if manual_override is not None:
        metadata = dict(doc.metadata_ or {})
        metadata["manual_doc_type_override"] = bool(manual_override)
        doc.metadata_ = metadata
    if "doc_type" in update_data and doc.doc_type and manual_override is not False:
        doc.doc_type_confidence = 1.0
        metadata = dict(doc.metadata_ or {})
        metadata["manual_doc_type_override"] = True
        doc.metadata_ = metadata

    await log_action(
        db,
        action="doc.update",
        entity_type="document",
        entity_id=doc.id,
        details=update_data,
    )
    await db.commit()
    await db.refresh(doc)

    # On approval: create Invoice/Party records, build memory graph, queue embeddings
    if update_data.get("status") == "approved":
        try:
            from app.tasks.extraction import process_approved_document
            process_approved_document.delay(str(doc.id))
            logger.info("post_approve_queued", document_id=str(doc.id))
        except Exception as e:
            logger.warning("post_approve_queue_failed", document_id=str(doc.id), error=str(e))

    return doc


# ── doc.link ─────────────────────────────────────────────────────────────────


@router.post("/{document_id}/links", response_model=DocumentLinkOut, status_code=201)
async def link_document(
    document_id: uuid.UUID,
    payload: DocumentLinkCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.link — Link document to an entity."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    link = DocumentLink(
        document_id=document_id,
        linked_entity_type=payload.linked_entity_type,
        linked_entity_id=payload.linked_entity_id,
        link_type=payload.link_type,
    )
    db.add(link)

    await log_action(
        db,
        action="doc.link",
        entity_type="document",
        entity_id=document_id,
        details={
            "linked_entity_type": payload.linked_entity_type,
            "linked_entity_id": str(payload.linked_entity_id),
        },
    )
    await db.commit()
    await db.refresh(link)
    return link


@router.patch("/{document_id}/links/{link_id}", response_model=DocumentLinkOut)
async def update_document_link(
    document_id: uuid.UUID,
    link_id: uuid.UUID,
    payload: DocumentLinkUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.link_update — Edit an explicit document dependency link."""
    result = await db.execute(
        select(DocumentLink).where(
            DocumentLink.id == link_id,
            DocumentLink.document_id == document_id,
        )
    )
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Document link not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(link, field, value)

    await log_action(
        db,
        action="doc.link_update",
        entity_type="document",
        entity_id=document_id,
        details={"link_id": str(link_id), **{k: str(v) for k, v in update_data.items()}},
    )
    await db.commit()
    await db.refresh(link)
    return link


@router.delete("/{document_id}/links/{link_id}", status_code=204)
async def delete_document_link(
    document_id: uuid.UUID,
    link_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.link_delete — Remove an explicit document dependency link."""
    result = await db.execute(
        select(DocumentLink).where(
            DocumentLink.id == link_id,
            DocumentLink.document_id == document_id,
        )
    )
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Document link not found")

    await db.delete(link)
    await log_action(
        db,
        action="doc.link_delete",
        entity_type="document",
        entity_id=document_id,
        details={"link_id": str(link_id)},
    )
    await db.commit()


@router.get("/{document_id}/dependencies", response_model=DocumentDependenciesResponse)
async def get_document_dependencies(
    document_id: uuid.UUID,
    query: str | None = None,
    edge_type: str | None = None,
    depth: int = Query(1, ge=1, le=3),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.dependencies — Search explicit links and graph dependencies for a document."""
    result = await db.execute(
        select(Document)
        .where(Document.id == document_id)
        .options(selectinload(Document.links))
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    pattern = f"%{query.strip()}%" if query and query.strip() else None
    node_query = select(KnowledgeNode).where(KnowledgeNode.source_document_id == document_id)
    if pattern:
        node_query = node_query.where(
            or_(
                KnowledgeNode.title.ilike(pattern),
                KnowledgeNode.summary.ilike(pattern),
                KnowledgeNode.canonical_key.ilike(pattern),
            )
        )
    node_result = await db.execute(
        node_query.order_by(KnowledgeNode.created_at.desc()).limit(limit)
    )
    nodes = list(node_result.scalars().all())

    node_ids = {node.id for node in nodes}
    frontier = set(node_ids)
    edge_ids: set[uuid.UUID] = set()
    for _ in range(depth):
        edge_query = select(KnowledgeEdge).where(
            or_(
                KnowledgeEdge.source_document_id == document_id,
                KnowledgeEdge.source_node_id.in_(frontier) if frontier else False,
                KnowledgeEdge.target_node_id.in_(frontier) if frontier else False,
            )
        )
        if edge_type:
            edge_query = edge_query.where(KnowledgeEdge.edge_type == edge_type)
        if pattern:
            edge_query = edge_query.where(
                or_(
                    KnowledgeEdge.edge_type.ilike(pattern),
                    KnowledgeEdge.reason.ilike(pattern),
                )
            )
        edge_result = await db.execute(edge_query.limit(limit))
        found_edges = list(edge_result.scalars().all())
        next_frontier: set[uuid.UUID] = set()
        for edge in found_edges:
            if edge.id in edge_ids:
                continue
            edge_ids.add(edge.id)
            for candidate_id in (edge.source_node_id, edge.target_node_id):
                if candidate_id not in node_ids:
                    node_ids.add(candidate_id)
                    next_frontier.add(candidate_id)
        if not next_frontier:
            break
        frontier = next_frontier

    edges = []
    if edge_ids:
        edge_result = await db.execute(select(KnowledgeEdge).where(KnowledgeEdge.id.in_(edge_ids)))
        edges = list(edge_result.scalars().all())
    if node_ids:
        nodes_result = await db.execute(select(KnowledgeNode).where(KnowledgeNode.id.in_(node_ids)))
        nodes = list(nodes_result.scalars().all())

    return DocumentDependenciesResponse(
        document_id=document_id,
        query=query,
        nodes=nodes,
        edges=edges,
        links=doc.links,
        total_nodes=len(nodes),
        total_edges=len(edges),
    )


# ── doc.classify ────────────────────────────────────────────────────────────


@router.post("/{document_id}/classify", response_model=TaskResponse)
async def classify_document(
    document_id: uuid.UUID,
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.classify — Trigger document classification via AI."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if not force and doc.status not in (DocumentStatus.ingested, DocumentStatus.needs_review):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot classify document in status {doc.status.value}",
        )

    from app.tasks.extraction import classify_document as classify_task

    job = await _create_processing_job(
        db,
        doc,
        status="queued",
        current_step="classification",
        memory_seed_done=True,
    )
    task = classify_task.delay(str(document_id), force)
    job.celery_task_id = str(task.id) if task else None

    await log_action(
        db,
        action="doc.classify",
        entity_type="document",
        entity_id=document_id,
    )
    await add_timeline_event(
        db,
        entity_type="document",
        entity_id=document_id,
        event_type="classify_started",
        summary="Document classification started",
        actor="system",
    )
    await db.commit()

    logger.info("classify_triggered", document_id=str(document_id), task_id=task.id)
    return TaskResponse(
        task_id=task.id,
        document_id=document_id,
        status="queued",
    )


# ── doc.extract ─────────────────────────────────────────────────────────────


@router.post("/{document_id}/extract", response_model=TaskResponse)
async def extract_document(
    document_id: uuid.UUID,
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.extract — Trigger full extraction pipeline (classify → extract → validate)."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    from app.tasks.extraction import process_document

    job = await _create_processing_job(
        db,
        doc,
        status="queued",
        current_step="classification",
        memory_seed_done=True,
    )
    task = process_document.delay(str(document_id), force)
    job.celery_task_id = str(task.id) if task else None

    await log_action(
        db,
        action="doc.extract",
        entity_type="document",
        entity_id=document_id,
    )
    await add_timeline_event(
        db,
        entity_type="document",
        entity_id=document_id,
        event_type="extraction_started",
        summary="Document extraction pipeline started",
        actor="system",
    )
    await db.commit()

    logger.info("extract_triggered", document_id=str(document_id), task_id=task.id)
    return TaskResponse(
        task_id=task.id,
        document_id=document_id,
        status="queued",
    )


# ── doc.memory_rebuild ──────────────────────────────────────────────────────


@router.post("/{document_id}/memory/rebuild", response_model=DocumentPipelineStatus)
async def rebuild_document_memory(
    document_id: uuid.UUID,
    build_scope: str | None = Query(None, pattern="^(compact|extended|ntd|drawing)$"),
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.memory_rebuild — Rebuild graph memory for one document."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    text = await _document_text_for_memory(db, doc)
    try:
        from app.domain.memory_builder import build_document_memory_async

        memory_result = await build_document_memory_async(
            db,
            doc,
            text=text,
            build_scope=build_scope,
            actor="user",
            clear_existing=True,
        )
        await add_timeline_event(
            db,
            entity_type="document",
            entity_id=doc.id,
            event_type="memory_rebuilt",
            summary="Document memory and graph links rebuilt",
            actor="user",
            details=memory_result.__dict__,
        )
        await log_action(
            db,
            action="doc.memory_rebuild",
            entity_type="document",
            entity_id=doc.id,
            details=memory_result.__dict__,
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Memory rebuild failed: {exc}") from exc

    return (await get_document_management_summary(document_id, db)).pipeline


# ── doc.delete ──────────────────────────────────────────────────────────────


@router.delete("/{document_id}", response_model=DocumentDeleteResult)
async def delete_document_hard(
    document_id: uuid.UUID,
    delete_files: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.delete — Hard-delete a document and all derived records."""
    result = await hard_delete_document(db, document_id, delete_files=delete_files)
    if int(result.get("missing") or 0):
        raise HTTPException(status_code=404, detail="Document not found")
    await db.commit()
    return _delete_result_payload(result)


# ── doc.correct_field ───────────────────────────────────────────────────────


@router.post("/{document_id}/correct-field", response_model=FieldCorrectionResponse)
async def correct_extraction_field(
    document_id: uuid.UUID,
    payload: FieldCorrectionRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.correct_field — Human correction of an extracted field."""
    from app.db.models import DocumentExtraction, ExtractionField

    # Get latest extraction for this document
    result = await db.execute(
        select(DocumentExtraction)
        .where(DocumentExtraction.document_id == document_id)
        .order_by(DocumentExtraction.created_at.desc())
        .limit(1)
    )
    extraction = result.scalar_one_or_none()
    if not extraction:
        raise HTTPException(status_code=404, detail="No extraction found for document")

    # Find the field
    result = await db.execute(
        select(ExtractionField).where(
            ExtractionField.extraction_id == extraction.id,
            ExtractionField.field_name == payload.field_name,
        )
    )
    field = result.scalar_one_or_none()
    if not field:
        raise HTTPException(
            status_code=404,
            detail=f"Field '{payload.field_name}' not found in extraction",
        )

    old_value = field.field_value
    field.human_corrected = True
    field.corrected_value = payload.corrected_value

    await log_action(
        db,
        action="doc.correct_field",
        entity_type="document",
        entity_id=document_id,
        details={
            "field_name": payload.field_name,
            "old_value": old_value,
            "corrected_value": payload.corrected_value,
        },
    )
    await add_timeline_event(
        db,
        entity_type="document",
        entity_id=document_id,
        event_type="field_corrected",
        summary=f"Field '{payload.field_name}' corrected by human",
        actor="user",
    )
    await db.commit()

    return FieldCorrectionResponse(
        field_name=payload.field_name,
        old_value=old_value,
        corrected_value=payload.corrected_value,
        extraction_id=extraction.id,
    )


# ── doc.summarize ───────────────────────────────────────────────────────────


@router.post("/{document_id}/summarize", response_model=DocumentSummaryAI)
async def summarize_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.summarize — Generate AI summary of a document."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Get document text
    text = ""
    if doc.mime_type == "application/pdf":
        try:
            from app.ai.pdf_processor import extract_pdf
            from app.storage import download_file

            content = download_file(doc.storage_path)
            pdf_data = extract_pdf(content, render_pages=False)
            text = pdf_data.full_text
        except Exception:
            pass

    if not text:
        # Try from latest extraction raw output
        from app.db.models import DocumentExtraction

        ext_result = await db.execute(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id == document_id)
            .order_by(DocumentExtraction.created_at.desc())
            .limit(1)
        )
        ext = ext_result.scalar_one_or_none()
        if ext and ext.raw_output:
            text = str(ext.raw_output)

    if not text:
        raise HTTPException(status_code=422, detail="No text available for summarization")

    from app.ai.router import ai_router

    summary_data = await ai_router.summarize_document(text)

    await log_action(
        db,
        action="doc.summarize",
        entity_type="document",
        entity_id=document_id,
    )
    await db.commit()

    return DocumentSummaryAI(
        document_id=document_id,
        summary=summary_data.get("summary", ""),
        key_facts=summary_data.get("key_facts", []),
        action_required=summary_data.get("action_required"),
        urgency=summary_data.get("urgency", "low"),
    )


# ── POST /api/documents/{id}/snooze ──────────────────────────────────────────


class SnoozeRequest(BaseModel):
    until: datetime
    reason: str | None = None


@router.post("/{document_id}/snooze", status_code=204)
async def snooze_document(
    document_id: uuid.UUID,
    payload: SnoozeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Snooze a document — hide from inbox until the given datetime."""
    from app.db.models import Snooze

    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Document not found")

    snooze = Snooze(
        entity_type="document",
        entity_id=document_id,
        user_id="system",
        until=payload.until,
        reason=payload.reason,
    )
    db.add(snooze)
    await log_action(
        db,
        action="doc.snooze",
        entity_type="document",
        entity_id=document_id,
        details={"until": payload.until.isoformat(), "reason": payload.reason},
    )
    await db.commit()
