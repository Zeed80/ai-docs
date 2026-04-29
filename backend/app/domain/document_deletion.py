"""Hard-delete helpers for documents and development cleanup."""

import uuid
from collections.abc import Iterable

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BOM,
    AnomalyCard,
    AuditLog,
    AuditTimelineEvent,
    BOMLine,
    CollectionItem,
    Document,
    DocumentArtifact,
    DocumentChunk,
    DocumentExtraction,
    DocumentLink,
    DocumentProcessingJob,
    DocumentVersion,
    EntityMention,
    EvidenceSpan,
    ExtractionField,
    GraphBuildStatus,
    GraphReviewItem,
    Invoice,
    InvoiceLine,
    KnowledgeEdge,
    KnowledgeNode,
    ManufacturingCheckResult,
    ManufacturingNormEstimate,
    ManufacturingOperation,
    ManufacturingProcessPlan,
    MemoryEmbeddingRecord,
    NormativeClause,
    NormativeDocument,
    NormativeDocumentVersion,
    NormativeRequirement,
    NTDCheckFinding,
    NTDCheckRun,
    PaymentSchedule,
    PriceHistoryEntry,
    QuarantineEntry,
    SupplierContract,
    TechnologyCorrection,
    WarehouseReceipt,
    WarehouseReceiptLine,
)

logger = structlog.get_logger()


async def hard_delete_document(
    db: AsyncSession,
    document_id: uuid.UUID,
    *,
    delete_files: bool = True,
) -> dict[str, int | str]:
    """Delete one document and all records directly derived from it."""
    doc = await db.get(Document, document_id)
    if not doc:
        return {"document_id": str(document_id), "deleted": 0, "missing": 1}

    storage_paths = [doc.storage_path]
    artifact_paths = (
        await db.execute(
            select(DocumentArtifact.storage_path).where(DocumentArtifact.document_id == document_id)
        )
    ).scalars().all()
    storage_paths.extend([path for path in artifact_paths if path])

    version_ids = set(
        (
            await db.execute(
                select(DocumentVersion.id).where(DocumentVersion.document_id == document_id)
            )
        ).scalars().all()
    )
    extraction_ids = set(
        (
            await db.execute(
                select(DocumentExtraction.id).where(DocumentExtraction.document_id == document_id)
            )
        ).scalars().all()
    )
    invoice_ids = set(
        (
            await db.execute(select(Invoice.id).where(Invoice.document_id == document_id))
        ).scalars().all()
    )
    invoice_line_ids = set()
    if invoice_ids:
        invoice_line_ids = set(
            (
                await db.execute(
                    select(InvoiceLine.id).where(InvoiceLine.invoice_id.in_(invoice_ids))
                )
            ).scalars().all()
        )
    receipt_ids = set(
        (
            await db.execute(
                select(WarehouseReceipt.id).where(WarehouseReceipt.document_id == document_id)
            )
        ).scalars().all()
    )
    if invoice_ids:
        receipt_ids.update(
            (
                await db.execute(
                    select(WarehouseReceipt.id).where(WarehouseReceipt.invoice_id.in_(invoice_ids))
                )
            ).scalars().all()
        )

    bom_ids = set(
        (await db.execute(select(BOM.id).where(BOM.document_id == document_id))).scalars().all()
    )
    process_plan_ids = set(
        (
            await db.execute(
                select(ManufacturingProcessPlan.id).where(
                    ManufacturingProcessPlan.document_id == document_id
                )
            )
        ).scalars().all()
    )
    operation_ids = set()
    if process_plan_ids:
        operation_ids = set(
            (
                await db.execute(
                    select(ManufacturingOperation.id).where(
                        ManufacturingOperation.process_plan_id.in_(process_plan_ids)
                    )
                )
            ).scalars().all()
        )

    normative_doc_ids = set(
        (
            await db.execute(
                select(NormativeDocument.id).where(
                    NormativeDocument.source_document_id == document_id
                )
            )
        ).scalars().all()
    )
    normative_version_ids = set(
        (
            await db.execute(
                select(NormativeDocumentVersion.id).where(
                    NormativeDocumentVersion.source_document_id == document_id
                )
            )
        ).scalars().all()
    )
    if normative_doc_ids:
        normative_version_ids.update(
            (
                await db.execute(
                    select(NormativeDocumentVersion.id).where(
                        NormativeDocumentVersion.normative_document_id.in_(normative_doc_ids)
                    )
                )
            ).scalars().all()
        )
    ntd_check_ids = set(
        (
            await db.execute(select(NTDCheckRun.id).where(NTDCheckRun.document_id == document_id))
        ).scalars().all()
    )

    chunk_ids = set(
        (
            await db.execute(
                select(DocumentChunk.id).where(DocumentChunk.document_id == document_id)
            )
        ).scalars().all()
    )
    evidence_ids = set(
        (
            await db.execute(select(EvidenceSpan.id).where(EvidenceSpan.document_id == document_id))
        ).scalars().all()
    )
    node_ids = set(
        (
            await db.execute(
                select(KnowledgeNode.id).where(
                    (KnowledgeNode.source_document_id == document_id)
                    | (
                        (KnowledgeNode.entity_type == "document")
                        & (KnowledgeNode.entity_id == document_id)
                    )
                )
            )
        ).scalars().all()
    )
    mention_ids = set(
        (
            await db.execute(
                select(EntityMention.id).where(EntityMention.document_id == document_id)
            )
        ).scalars().all()
    )
    embedding_points = (
        await db.execute(
            select(
                MemoryEmbeddingRecord.collection_name,
                MemoryEmbeddingRecord.point_id,
            ).where(MemoryEmbeddingRecord.document_id == document_id)
        )
    ).all()

    edge_ids = set(
        (
            await db.execute(
                select(KnowledgeEdge.id).where(KnowledgeEdge.source_document_id == document_id)
            )
        ).scalars().all()
    )
    if node_ids:
        edge_ids.update(
            (
                await db.execute(
                    select(KnowledgeEdge.id).where(
                        (KnowledgeEdge.source_node_id.in_(node_ids))
                        | (KnowledgeEdge.target_node_id.in_(node_ids))
                    )
                )
            ).scalars().all()
        )
    if evidence_ids:
        edge_ids.update(
            (
                await db.execute(
                    select(KnowledgeEdge.id).where(KnowledgeEdge.evidence_span_id.in_(evidence_ids))
                )
            ).scalars().all()
        )

    counts: dict[str, int | str] = {"document_id": str(document_id)}

    async def remove(model, *conditions) -> None:
        if not conditions:
            return
        result = await db.execute(delete(model).where(*conditions))
        counts[model.__tablename__] = int(result.rowcount or 0)

    await remove(GraphReviewItem, GraphReviewItem.document_id == document_id)
    if edge_ids:
        await remove(GraphReviewItem, GraphReviewItem.edge_id.in_(edge_ids))
    if node_ids:
        await remove(GraphReviewItem, GraphReviewItem.node_id.in_(node_ids))
    if mention_ids:
        await remove(GraphReviewItem, GraphReviewItem.mention_id.in_(mention_ids))

    await remove(MemoryEmbeddingRecord, MemoryEmbeddingRecord.document_id == document_id)
    if version_ids:
        await remove(
            MemoryEmbeddingRecord,
            MemoryEmbeddingRecord.document_version_id.in_(version_ids),
        )
    if edge_ids:
        await remove(KnowledgeEdge, KnowledgeEdge.id.in_(edge_ids))
    if mention_ids:
        await remove(EntityMention, EntityMention.id.in_(mention_ids))
    if node_ids:
        await remove(KnowledgeNode, KnowledgeNode.id.in_(node_ids))
    if evidence_ids:
        await remove(KnowledgeEdge, KnowledgeEdge.evidence_span_id.in_(evidence_ids))
        await remove(EvidenceSpan, EvidenceSpan.id.in_(evidence_ids))
    if chunk_ids:
        await remove(DocumentChunk, DocumentChunk.id.in_(chunk_ids))

    await remove(GraphBuildStatus, GraphBuildStatus.document_id == document_id)
    await remove(DocumentProcessingJob, DocumentProcessingJob.document_id == document_id)
    await remove(DocumentArtifact, DocumentArtifact.document_id == document_id)
    await remove(DocumentLink, DocumentLink.document_id == document_id)
    await remove(QuarantineEntry, QuarantineEntry.document_id == document_id)

    if ntd_check_ids:
        await remove(NTDCheckFinding, NTDCheckFinding.check_id.in_(ntd_check_ids))
    await remove(NTDCheckFinding, NTDCheckFinding.document_id == document_id)
    await remove(NTDCheckRun, NTDCheckRun.document_id == document_id)

    if normative_doc_ids:
        await remove(NTDCheckFinding, NTDCheckFinding.normative_document_id.in_(normative_doc_ids))
        await remove(
            NormativeRequirement,
            NormativeRequirement.normative_document_id.in_(normative_doc_ids),
        )
        await remove(NormativeClause, NormativeClause.normative_document_id.in_(normative_doc_ids))
        await remove(
            NormativeDocumentVersion,
            NormativeDocumentVersion.normative_document_id.in_(normative_doc_ids),
        )
        await remove(NormativeDocument, NormativeDocument.id.in_(normative_doc_ids))
    if normative_version_ids:
        await remove(
            NormativeDocumentVersion,
            NormativeDocumentVersion.id.in_(normative_version_ids),
        )

    if process_plan_ids:
        await remove(
            ManufacturingCheckResult,
            ManufacturingCheckResult.process_plan_id.in_(process_plan_ids),
        )
        await remove(
            ManufacturingNormEstimate,
            ManufacturingNormEstimate.process_plan_id.in_(process_plan_ids),
        )
    if operation_ids:
        await remove(
            ManufacturingCheckResult,
            ManufacturingCheckResult.operation_id.in_(operation_ids),
        )
        await remove(
            ManufacturingNormEstimate,
            ManufacturingNormEstimate.operation_id.in_(operation_ids),
        )
        await remove(ManufacturingOperation, ManufacturingOperation.id.in_(operation_ids))
    if process_plan_ids:
        await remove(
            TechnologyCorrection,
            TechnologyCorrection.process_plan_id.in_(process_plan_ids),
        )
        await remove(ManufacturingProcessPlan, ManufacturingProcessPlan.id.in_(process_plan_ids))
    await remove(TechnologyCorrection, TechnologyCorrection.source_document_id == document_id)

    if bom_ids:
        await remove(BOMLine, BOMLine.bom_id.in_(bom_ids))
        await remove(BOM, BOM.id.in_(bom_ids))

    if receipt_ids:
        await remove(WarehouseReceiptLine, WarehouseReceiptLine.receipt_id.in_(receipt_ids))
        await remove(WarehouseReceipt, WarehouseReceipt.id.in_(receipt_ids))

    if invoice_ids:
        await remove(PaymentSchedule, PaymentSchedule.invoice_id.in_(invoice_ids))
        await remove(PriceHistoryEntry, PriceHistoryEntry.invoice_id.in_(invoice_ids))
    if invoice_line_ids:
        await remove(PriceHistoryEntry, PriceHistoryEntry.invoice_line_id.in_(invoice_line_ids))
        await remove(
            WarehouseReceiptLine,
            WarehouseReceiptLine.invoice_line_id.in_(invoice_line_ids),
        )
        await remove(InvoiceLine, InvoiceLine.id.in_(invoice_line_ids))
    if invoice_ids:
        await remove(Invoice, Invoice.id.in_(invoice_ids))

    if extraction_ids:
        await remove(ExtractionField, ExtractionField.extraction_id.in_(extraction_ids))
        await remove(DocumentExtraction, DocumentExtraction.id.in_(extraction_ids))

    await remove(SupplierContract, SupplierContract.document_id == document_id)
    await remove(WarehouseReceipt, WarehouseReceipt.document_id == document_id)
    await remove(BOM, BOM.document_id == document_id)
    await remove(ManufacturingProcessPlan, ManufacturingProcessPlan.document_id == document_id)

    await _delete_cross_entity_records(db, document_id, invoice_ids, bom_ids, counts)

    if version_ids:
        await remove(DocumentVersion, DocumentVersion.id.in_(version_ids))
    result = await db.execute(delete(Document).where(Document.id == document_id))
    counts["documents"] = int(result.rowcount or 0)
    counts["deleted"] = counts["documents"]

    if delete_files:
        counts["storage_deleted"] = _delete_storage_paths(storage_paths)

    _delete_qdrant_document(document_id)
    _delete_qdrant_memory_points(embedding_points)
    logger.info("document_hard_deleted", document_id=str(document_id), counts=counts)
    return counts


async def hard_delete_documents(
    db: AsyncSession,
    document_ids: Iterable[uuid.UUID],
    *,
    delete_files: bool = True,
) -> dict:
    results = []
    deleted = 0
    missing = 0
    for document_id in document_ids:
        result = await hard_delete_document(db, document_id, delete_files=delete_files)
        deleted += int(result.get("deleted") or 0)
        missing += int(result.get("missing") or 0)
        results.append(result)
    return {"deleted": deleted, "missing": missing, "results": results}


async def purge_all_development_data(
    db: AsyncSession,
    *,
    delete_files: bool = True,
) -> dict:
    document_ids = list((await db.execute(select(Document.id))).scalars().all())
    result = await hard_delete_documents(db, document_ids, delete_files=delete_files)
    result["documents_seen"] = len(document_ids)
    return result


async def _delete_cross_entity_records(
    db: AsyncSession,
    document_id: uuid.UUID,
    invoice_ids: set[uuid.UUID],
    bom_ids: set[uuid.UUID],
    counts: dict[str, int | str],
) -> None:
    entity_pairs = [("document", document_id)]
    entity_pairs.extend(("invoice", invoice_id) for invoice_id in invoice_ids)
    entity_pairs.extend(("bom", bom_id) for bom_id in bom_ids)

    for entity_type, entity_id in entity_pairs:
        for model in (CollectionItem, AnomalyCard, AuditLog, AuditTimelineEvent):
            column_type = getattr(model, "entity_type", None)
            column_id = getattr(model, "entity_id", None)
            if column_type is None or column_id is None:
                continue
            result = await db.execute(
                delete(model).where(column_type == entity_type, column_id == entity_id)
            )
            key = model.__tablename__
            counts[key] = int(counts.get(key, 0) or 0) + int(result.rowcount or 0)


def _delete_storage_paths(storage_paths: Iterable[str]) -> int:
    try:
        from app.storage import delete_file
    except Exception:
        return 0

    deleted = 0
    for path in {path for path in storage_paths if path}:
        try:
            delete_file(path)
            deleted += 1
        except Exception as exc:
            logger.warning("document_storage_delete_failed", path=path, error=str(exc))
    return deleted


def _delete_qdrant_document(document_id: uuid.UUID) -> None:
    try:
        from app.vector.qdrant_store import delete_document

        delete_document(str(document_id))
    except Exception as exc:
        logger.warning(
            "document_qdrant_delete_failed",
            document_id=str(document_id),
            error=str(exc),
        )


def _delete_qdrant_memory_points(points: Iterable[tuple[str, str]]) -> None:
    grouped: dict[str, list[str]] = {}
    for collection_name, point_id in points:
        if not collection_name or not point_id:
            continue
        grouped.setdefault(collection_name, []).append(point_id)
    if not grouped:
        return

    try:
        from qdrant_client.models import PointIdsList

        from app.vector.qdrant_store import _stable_point_uuid, get_client

        client = get_client()
        for collection_name, point_ids in grouped.items():
            client.delete(
                collection_name=collection_name,
                points_selector=PointIdsList(
                    points=[_stable_point_uuid(point_id) for point_id in point_ids]
                ),
            )
    except Exception as exc:
        logger.warning("document_qdrant_memory_delete_failed", error=str(exc))
