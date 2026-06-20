"""Hard-delete helpers for documents and development cleanup."""

import uuid
from collections.abc import Iterable

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BOM,
    Approval,
    AnomalyCard,
    AuditLog,
    AuditTimelineEvent,
    BOMLine,
    CollectionItem,
    Document,
    DraftAction,
    DocumentArtifact,
    DocumentChunk,
    DocumentExtraction,
    DocumentLink,
    DocumentProcessingJob,
    DocumentVersion,
    DraftEmail,
    Drawing,
    DrawingAssemblyBOM,
    DrawingFeature,
    DrawingFeatureCorrection,
    DrawingTPLink,
    DrawingViewSection,
    EmailThread,
    EntityMention,
    EvidenceSpan,
    ExportJob,
    ExtractionField,
    FeatureToolBinding,
    GraphBuildStatus,
    GraphReviewItem,
    InventoryItem,
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
    Party,
    PaymentSchedule,
    PriceHistoryEntry,
    PurchaseRequest,
    QuarantineEntry,
    StockMovement,
    SupplierContract,
    SupplierProfile,
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

    # Canonical graph nodes (supplier, invoice, tool, standard…) deliberately
    # carry no source_document_id so they outlive single-document deletes. Once
    # this document's edges/mentions are gone, any node left with no edges and no
    # mentions is an orphan and must be cleaned too. Collect every node touched by
    # the deleted subgraph (edge endpoints + mention targets + this doc's nodes)
    # as cleanup candidates; the actual orphan delete runs after the edge/mention
    # removals below.
    orphan_candidate_ids: set[uuid.UUID] = set(node_ids)
    if edge_ids:
        for source_node_id, target_node_id in (
            await db.execute(
                select(KnowledgeEdge.source_node_id, KnowledgeEdge.target_node_id).where(
                    KnowledgeEdge.id.in_(edge_ids)
                )
            )
        ).all():
            orphan_candidate_ids.add(source_node_id)
            orphan_candidate_ids.add(target_node_id)
    if mention_ids:
        orphan_candidate_ids.update(
            node_id
            for node_id in (
                await db.execute(
                    select(EntityMention.node_id).where(EntityMention.id.in_(mention_ids))
                )
            ).scalars().all()
            if node_id
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

    # Drop canonical nodes that became orphaned by the edge/mention removals above
    # (no remaining edges, no remaining mentions). Nodes still referenced by other
    # documents keep their edges and are preserved.
    await _delete_orphan_nodes(db, orphan_candidate_ids - node_ids, counts)

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

    # ── Drawing cascade ──────────────────────────────────────────────────────
    # Must happen before deleting Document (drawings.document_id → documents.id)
    drawing_ids = set(
        (
            await db.execute(
                select(Drawing.id).where(Drawing.document_id == document_id)
            )
        ).scalars().all()
    )
    if drawing_ids:
        # Nullify nullable drawing_id FK in process plans from *other* documents
        await db.execute(
            ManufacturingProcessPlan.__table__.update()  # type: ignore[attr-defined]
            .where(ManufacturingProcessPlan.drawing_id.in_(drawing_ids))
            .values(drawing_id=None)
        )
        # Delete child tables of Drawing (all have NOT NULL FK to drawings.id)
        await remove(DrawingFeature, DrawingFeature.drawing_id.in_(drawing_ids))
        await remove(DrawingViewSection, DrawingViewSection.drawing_id.in_(drawing_ids))
        await remove(DrawingAssemblyBOM, DrawingAssemblyBOM.drawing_id.in_(drawing_ids))
        await remove(DrawingFeatureCorrection, DrawingFeatureCorrection.drawing_id.in_(drawing_ids))
        await remove(DrawingTPLink, DrawingTPLink.drawing_id.in_(drawing_ids))
        await remove(Drawing, Drawing.id.in_(drawing_ids))

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

    # Also purge supplier-related data (not derived from documents)
    await _purge_supplier_data(db)

    # Deterministically wipe any remaining knowledge-graph rows. Canonical nodes
    # (supplier/invoice/tool/…) carry no source_document_id, so the per-document
    # cascade cannot reach the ones not tied to a deleted edge; a full purge must
    # leave the graph empty.
    await _purge_graph_data(db)

    # Warehouse stock (inventory_items, stock_movements) is not derived from
    # documents and survives the per-document cascade — wipe it too so a full
    # purge leaves the warehouse empty.
    warehouse = await purge_warehouse_data(db)
    result["warehouse"] = warehouse

    return result


async def _purge_graph_data(db: AsyncSession) -> None:
    """Delete all knowledge-graph rows (FK-safe order)."""
    for model in (GraphReviewItem, KnowledgeEdge, EntityMention, KnowledgeNode):
        await db.execute(delete(model))
    logger.info("graph_data_purged")


async def purge_warehouse_data(db: AsyncSession) -> dict[str, int]:
    """Delete all warehouse data: receipts, stock items and movements.

    Standalone helper so it can back both the full purge and a warehouse-only
    clear. Caller is responsible for committing. Canonical normalization catalog
    (``canonical_items``) is intentionally preserved — it is not warehouse stock.
    """
    # Nullify the only nullable FK pointing at inventory items from outside the
    # warehouse domain so the inventory delete cannot violate it.
    await db.execute(
        FeatureToolBinding.__table__.update()  # type: ignore[attr-defined]
        .where(FeatureToolBinding.warehouse_item_id.isnot(None))
        .values(warehouse_item_id=None)
    )
    counts: dict[str, int] = {}
    # FK-safe order: movements & receipt lines reference inventory items/receipts.
    for model in (
        StockMovement,
        WarehouseReceiptLine,
        WarehouseReceipt,
        InventoryItem,
    ):
        result = await db.execute(delete(model))
        counts[model.__tablename__] = int(result.rowcount or 0)
    logger.info("warehouse_data_purged", counts=counts)
    return counts


async def _purge_supplier_data(db: AsyncSession) -> None:
    """Delete all supplier/party records not tied to specific documents."""
    # Nullify nullable FKs that reference parties (avoids FK constraint errors)
    await db.execute(
        EmailThread.__table__.update().values(party_id=None)  # type: ignore[attr-defined]
    )
    await db.execute(
        WarehouseReceipt.__table__.update().values(supplier_id=None)  # type: ignore[attr-defined]
    )
    # Delete in FK-safe order
    for model in (SupplierContract, PriceHistoryEntry, SupplierProfile, Party):
        await db.execute(delete(model))
    logger.info("supplier_data_purged")


async def _delete_orphan_nodes(
    db: AsyncSession,
    candidate_ids: set[uuid.UUID],
    counts: dict[str, int | str],
) -> None:
    """Delete knowledge nodes from ``candidate_ids`` left with no edges/mentions."""
    if not candidate_ids:
        return
    referenced: set[uuid.UUID] = set()
    referenced.update(
        (
            await db.execute(
                select(KnowledgeEdge.source_node_id).where(
                    KnowledgeEdge.source_node_id.in_(candidate_ids)
                )
            )
        ).scalars().all()
    )
    referenced.update(
        (
            await db.execute(
                select(KnowledgeEdge.target_node_id).where(
                    KnowledgeEdge.target_node_id.in_(candidate_ids)
                )
            )
        ).scalars().all()
    )
    referenced.update(
        (
            await db.execute(
                select(EntityMention.node_id).where(EntityMention.node_id.in_(candidate_ids))
            )
        ).scalars().all()
    )
    orphan_ids = {node_id for node_id in candidate_ids if node_id not in referenced}
    if not orphan_ids:
        return
    await db.execute(delete(GraphReviewItem).where(GraphReviewItem.node_id.in_(orphan_ids)))
    result = await db.execute(delete(KnowledgeNode).where(KnowledgeNode.id.in_(orphan_ids)))
    counts["knowledge_nodes_orphaned"] = int(
        counts.get("knowledge_nodes_orphaned", 0) or 0
    ) + int(result.rowcount or 0)


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

    # Collect the approval ids tied to these entities *before* deleting anything,
    # then expand with their chain children (self-referencing chain_root_id FK).
    root_approval_ids: set[uuid.UUID] = set()
    for entity_type, entity_id in entity_pairs:
        rows = (await db.execute(
            select(Approval.id).where(
                Approval.entity_type == entity_type,
                Approval.entity_id == entity_id,
            )
        )).scalars().all()
        root_approval_ids.update(rows)

    child_approval_ids: set[uuid.UUID] = set()
    if root_approval_ids:
        child_rows = (await db.execute(
            select(Approval.id).where(
                Approval.chain_root_id.in_(root_approval_ids),
                Approval.id.notin_(root_approval_ids),
            )
        )).scalars().all()
        child_approval_ids.update(child_rows)

    all_approval_ids = root_approval_ids | child_approval_ids

    # Null out FK references to the approvals from tables we are *not* deleting
    # here (export_jobs, draft_emails, purchase_requests, draft_actions), so the
    # subsequent approval delete does not violate those constraints.
    if all_approval_ids:
        for model in (ExportJob, DraftEmail, PurchaseRequest, DraftAction):
            await db.execute(
                model.__table__.update()  # type: ignore[attr-defined]
                .where(model.approval_id.in_(all_approval_ids))
                .values(approval_id=None)
            )
        # Delete chain children first; chain roots are removed by the loop below.
        if child_approval_ids:
            result = await db.execute(
                delete(Approval).where(Approval.id.in_(child_approval_ids))
            )
            counts[Approval.__tablename__] = int(
                counts.get(Approval.__tablename__, 0) or 0
            ) + int(result.rowcount or 0)

    # Remove export jobs / draft emails tied to the deleted entities so no rows
    # are left pointing at non-existent invoices/documents (orphan cleanup).
    for entity_type, entity_id in entity_pairs:
        export_result = await db.execute(
            delete(ExportJob).where(
                ExportJob.entity_type == entity_type,
                ExportJob.entity_id == entity_id,
            )
        )
        counts[ExportJob.__tablename__] = int(
            counts.get(ExportJob.__tablename__, 0) or 0
        ) + int(export_result.rowcount or 0)
        email_result = await db.execute(
            delete(DraftEmail).where(
                DraftEmail.related_entity_type == entity_type,
                DraftEmail.related_entity_id == entity_id,
            )
        )
        counts[DraftEmail.__tablename__] = int(
            counts.get(DraftEmail.__tablename__, 0) or 0
        ) + int(email_result.rowcount or 0)

    for entity_type, entity_id in entity_pairs:
        for model in (CollectionItem, AnomalyCard, Approval, DraftAction, AuditLog, AuditTimelineEvent):
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
