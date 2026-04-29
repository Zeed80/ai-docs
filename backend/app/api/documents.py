"""Document API — skills: doc.ingest, doc.get, doc.list, doc.update, doc.link"""

import hashlib
import uuid
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit.service import add_timeline_event, log_action
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
from app.db.session import get_db
from app.domain.document_deletion import (
    hard_delete_document,
    hard_delete_documents,
    purge_all_development_data,
)
from app.domain.documents import (
    DevelopmentPurgeRequest,
    DevelopmentPurgeResponse,
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
    FieldCorrectionRequest,
    FieldCorrectionResponse,
    TaskResponse,
)

router = APIRouter()
logger = structlog.get_logger()

DEFAULT_ALLOWED_EXTENSIONS = {
    ".bmp",
    ".csv",
    ".docx",
    ".dxf",
    ".iges",
    ".igs",
    ".jpeg",
    ".jpg",
    ".json",
    ".pdf",
    ".png",
    ".step",
    ".stp",
    ".tif",
    ".tiff",
    ".txt",
    ".xls",
    ".xlsx",
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

    disposition = "inline" if inline else f'attachment; filename="{doc.file_name}"'
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
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.ingest — Accept file, store, create Document record."""
    content = await file.read()
    file_hash = hashlib.sha256(content).hexdigest()
    file_size = len(content)

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

    initial_status = DocumentStatus.ingested if is_allowed else DocumentStatus.suspicious
    doc = Document(
        file_name=file.filename or "unknown",
        file_hash=file_hash,
        file_size=file_size,
        mime_type=file.content_type or "application/octet-stream",
        storage_path=storage_path,
        source_channel=source_channel,
        status=initial_status,
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
    await add_timeline_event(
        db,
        entity_type="document",
        entity_id=doc.id,
        event_type="ingested",
        summary=f"Document ingested: {doc.file_name}",
        actor="system",
    )

    try:
        from app.domain.memory_builder import build_document_memory_async

        memory_result = await build_document_memory_async(db, doc, text=doc.file_name)
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

    await db.commit()

    logger.info("document_ingested", doc_id=str(doc.id), file_name=doc.file_name)

    # Auto-trigger extraction pipeline
    try:
        from app.tasks.extraction import process_document
        process_document.delay(str(doc.id))
        logger.info("extraction_queued", doc_id=str(doc.id))
    except Exception as e:
        logger.warning("extraction_queue_failed", doc_id=str(doc.id), error=str(e))

    # Auto-trigger embedding (after extraction completes; also embed file_name immediately)
    try:
        from app.tasks.embedding import embed_document
        embed_document.apply_async(args=[str(doc.id)], countdown=5)
    except Exception as e:
        logger.warning("embed_queue_failed", doc_id=str(doc.id), error=str(e))

    return DocumentIngestResponse(
        id=doc.id,
        file_name=doc.file_name,
        file_hash=doc.file_hash,
        file_size=doc.file_size,
        mime_type=doc.mime_type,
        status=doc.status,
        created_at=doc.created_at,
    )


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


# ── doc.list ─────────────────────────────────────────────────────────────────


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    status: DocumentStatus | None = None,
    doc_type: DocumentType | None = None,
    source_channel: str | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
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

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Fetch
    query = query.order_by(Document.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    items = result.scalars().all()

    return DocumentListResponse(items=items, total=total, offset=offset, limit=limit)


@router.delete("/bulk-delete", response_model=DocumentBulkDeleteResponse)
async def bulk_delete_documents(
    payload: DocumentBulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
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


@router.post("/dev/purge-all", response_model=DevelopmentPurgeResponse)
async def purge_all_documents_for_development(
    payload: DevelopmentPurgeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Dev-only hard purge of documents and all derived DB records."""
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

    pipeline = DocumentPipelineStatus(
        processing_status=latest_job.status if latest_job else None,
        current_step=latest_job.current_step if latest_job else None,
        processing_error=latest_job.error if latest_job else None,
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
    for field, value in update_data.items():
        if field == "metadata_":
            doc.metadata_ = value
        else:
            setattr(doc, field, value)

    await log_action(
        db,
        action="doc.update",
        entity_type="document",
        entity_id=doc.id,
        details=update_data,
    )
    await db.commit()
    await db.refresh(doc)
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
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.classify — Trigger document classification via AI."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if doc.status not in (DocumentStatus.ingested, DocumentStatus.needs_review):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot classify document in status {doc.status.value}",
        )

    from app.tasks.extraction import classify_document as classify_task

    task = classify_task.delay(str(document_id))

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
    db: AsyncSession = Depends(get_db),
):
    """Skill: doc.extract — Trigger full extraction pipeline (classify → extract → validate)."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    from app.tasks.extraction import process_document

    task = process_document.delay(str(document_id))

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
