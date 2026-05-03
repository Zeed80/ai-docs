"""Graph ingestion utilities for drawings and tool catalog.

Integrates with KnowledgeNode/KnowledgeEdge (PostgreSQL graph) and Qdrant vectors.
"""

import uuid
import structlog
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Drawing,
    DrawingFeature,
    FeatureToolBinding,
    KnowledgeEdge,
    KnowledgeNode,
    ToolCatalogEntry,
    ToolSupplier,
)

logger = structlog.get_logger()


async def ingest_drawing_graph(drawing_id: uuid.UUID, db: AsyncSession) -> dict:
    """
    Build graph nodes for a drawing and all its features.
    
    Creates:
      KnowledgeNode(drawing) → KnowledgeNode(feature) edges via has_feature
      KnowledgeNode(drawing) → KnowledgeNode(document) edges via derived_from (if linked)
    
    Returns summary dict.
    """
    drawing = await db.get(Drawing, drawing_id)
    if not drawing:
        raise ValueError(f"Drawing {drawing_id} not found")

    result = await db.execute(
        select(DrawingFeature).where(DrawingFeature.drawing_id == drawing_id)
    )
    features = result.scalars().all()

    drawing_node = await _upsert_node(
        db=db,
        node_type="drawing",
        title=drawing.drawing_number or drawing.filename,
        canonical_key=f"drawing:{drawing_id}",
        entity_type="drawing",
        entity_id=drawing_id,
        summary=(
            f"Чертёж {drawing.drawing_number or drawing.filename} "
            f"(ред. {drawing.revision or '-'}). "
            f"Формат: {drawing.format}. "
            f"Статус: {drawing.status.value}"
        ),
        source_document_id=drawing.document_id,
    )

    if drawing.document_id:
        doc_node = await _get_or_create_document_node(db, drawing.document_id)
        if doc_node:
            await _upsert_edge(
                db=db,
                source_id=drawing_node.id,
                target_id=doc_node.id,
                edge_type="derived_from",
                reason="Чертёж привязан к документу",
            )

    feature_nodes_created = 0
    for feature in features:
        await _ingest_feature_node(db, feature, drawing_node)
        feature_nodes_created += 1

    await db.flush()
    logger.info(
        "drawing_graph_ingested",
        drawing_id=str(drawing_id),
        features=feature_nodes_created,
    )
    return {
        "drawing_node_id": str(drawing_node.id),
        "features_ingested": feature_nodes_created,
    }


async def _ingest_feature_node(
    db: AsyncSession,
    feature: DrawingFeature,
    drawing_node: KnowledgeNode,
) -> KnowledgeNode:
    """Create/update KnowledgeNode for a feature and link it to the drawing node."""
    dims_summary = ""
    if feature.dimensions:
        dim_parts = []
        for d in feature.dimensions[:3]:
            label = d.label or f"{d.nominal}{'+' + str(d.upper_tol) if d.upper_tol else ''}"
            dim_parts.append(label)
        dims_summary = ", ".join(dim_parts)

    surface_summary = ""
    if feature.surfaces:
        s = feature.surfaces[0]
        surface_summary = f"{s.roughness_type.value} {s.value}"

    summary_parts = [feature.name]
    if dims_summary:
        summary_parts.append(f"Размеры: {dims_summary}")
    if surface_summary:
        summary_parts.append(f"Шероховатость: {surface_summary}")
    if feature.description:
        summary_parts.append(feature.description[:200])

    feature_node = await _upsert_node(
        db=db,
        node_type="drawing_feature",
        title=feature.name,
        canonical_key=f"drawing_feature:{feature.id}",
        entity_type="drawing_feature",
        entity_id=feature.id,
        summary=". ".join(summary_parts),
        confidence=feature.confidence,
    )

    await _upsert_edge(
        db=db,
        source_id=drawing_node.id,
        target_id=feature_node.id,
        edge_type="has_feature",
        reason=f"Чертёж содержит элемент: {feature.name}",
        confidence=feature.confidence,
    )

    return feature_node


async def ingest_tool_catalog_graph(entry_id: uuid.UUID, db: AsyncSession) -> dict:
    """
    Build graph nodes for a tool catalog entry.
    
    Creates:
      KnowledgeNode(tool_catalog_entry) with supplier edge
    """
    entry = await db.get(ToolCatalogEntry, entry_id)
    if not entry:
        raise ValueError(f"ToolCatalogEntry {entry_id} not found")

    params_summary = ""
    if entry.parameters:
        params_parts = [f"{k}={v}" for k, v in list(entry.parameters.items())[:5]]
        params_summary = ", ".join(params_parts)

    summary = (
        f"{entry.tool_type.value}: {entry.name}. "
        + (f"Ø{entry.diameter_mm}мм. " if entry.diameter_mm else "")
        + (f"Материал: {entry.material}. " if entry.material else "")
        + (f"Покрытие: {entry.coating}. " if entry.coating else "")
        + (f"Параметры: {params_summary}" if params_summary else "")
    ).strip()

    tool_node = await _upsert_node(
        db=db,
        node_type="tool_catalog_entry",
        title=entry.name,
        canonical_key=f"tool_catalog:{entry_id}",
        entity_type="tool_catalog_entry",
        entity_id=entry_id,
        summary=summary,
    )

    if entry.supplier_id:
        supplier = await db.get(ToolSupplier, entry.supplier_id)
        if supplier:
            supplier_node = await _upsert_node(
                db=db,
                node_type="tool_supplier",
                title=supplier.name,
                canonical_key=f"tool_supplier:{supplier.id}",
                entity_type="tool_supplier",
                entity_id=supplier.id,
                summary=f"Поставщик инструментов: {supplier.name}",
            )
            await _upsert_edge(
                db=db,
                source_id=tool_node.id,
                target_id=supplier_node.id,
                edge_type="supplied_by",
                reason=f"Инструмент поставляется компанией {supplier.name}",
            )

    await db.flush()
    logger.info("tool_catalog_graph_ingested", entry_id=str(entry_id))
    return {"tool_node_id": str(tool_node.id)}


async def delete_drawing_graph(drawing_id: uuid.UUID, db: AsyncSession) -> int:
    """Delete KnowledgeNodes and edges for a drawing and all its features.

    Only removes nodes with entity_type in ('drawing', 'drawing_feature').
    Does not touch document nodes or tool supplier nodes even if linked.
    Returns count of deleted nodes.
    """
    entity_types = ("drawing", "drawing_feature")
    result = await db.execute(
        select(KnowledgeNode.id).where(
            KnowledgeNode.entity_type.in_(entity_types),
            KnowledgeNode.entity_id.in_(
                select(DrawingFeature.id).where(DrawingFeature.drawing_id == drawing_id)
            )
            | (
                (KnowledgeNode.entity_type == "drawing")
                & (KnowledgeNode.entity_id == drawing_id)
            ),
        )
    )
    node_ids = list(result.scalars().all())

    if not node_ids:
        return 0

    await db.execute(
        sa_delete(KnowledgeEdge).where(
            KnowledgeEdge.source_node_id.in_(node_ids)
            | KnowledgeEdge.target_node_id.in_(node_ids)
        )
    )
    await db.execute(sa_delete(KnowledgeNode).where(KnowledgeNode.id.in_(node_ids)))
    await db.flush()
    logger.info("drawing_graph_deleted", drawing_id=str(drawing_id), nodes=len(node_ids))
    return len(node_ids)


async def delete_tool_catalog_graph(entry_id: uuid.UUID, db: AsyncSession) -> int:
    """Delete KnowledgeNode and edges for a single tool catalog entry.

    Does not delete the tool_supplier node (it may have other entries).
    Returns count of deleted nodes.
    """
    result = await db.execute(
        select(KnowledgeNode.id).where(
            KnowledgeNode.entity_type == "tool_catalog_entry",
            KnowledgeNode.entity_id == entry_id,
        )
    )
    node_ids = list(result.scalars().all())
    if not node_ids:
        return 0

    await db.execute(
        sa_delete(KnowledgeEdge).where(
            KnowledgeEdge.source_node_id.in_(node_ids)
            | KnowledgeEdge.target_node_id.in_(node_ids)
        )
    )
    await db.execute(sa_delete(KnowledgeNode).where(KnowledgeNode.id.in_(node_ids)))
    await db.flush()
    logger.info("tool_catalog_graph_deleted", entry_id=str(entry_id))
    return len(node_ids)


async def delete_party_graph(party_id: uuid.UUID, db: AsyncSession) -> int:
    """Delete KnowledgeNodes for a party/supplier and all its tool catalog entries and suppliers.

    Cascades: deletes tool_supplier + tool_catalog_entry nodes linked to this party.
    Returns count of deleted nodes.
    """
    result = await db.execute(
        select(KnowledgeNode.id).where(
            KnowledgeNode.entity_id.in_(
                select(ToolCatalogEntry.id).join(
                    ToolSupplier, ToolSupplier.id == ToolCatalogEntry.supplier_id
                ).where(ToolSupplier.main_supplier_id == party_id)
            )
            | KnowledgeNode.entity_id.in_(
                select(ToolSupplier.id).where(ToolSupplier.main_supplier_id == party_id)
            )
        )
    )
    node_ids = list(result.scalars().all())

    if not node_ids:
        return 0

    await db.execute(
        sa_delete(KnowledgeEdge).where(
            KnowledgeEdge.source_node_id.in_(node_ids)
            | KnowledgeEdge.target_node_id.in_(node_ids)
        )
    )
    await db.execute(sa_delete(KnowledgeNode).where(KnowledgeNode.id.in_(node_ids)))
    await db.flush()
    logger.info("party_graph_deleted", party_id=str(party_id), nodes=len(node_ids))
    return len(node_ids)


async def delete_invoice_graph(invoice_id: uuid.UUID, db: AsyncSession) -> int:
    """Delete KnowledgeNodes and edges for an invoice."""
    result = await db.execute(
        select(KnowledgeNode.id).where(
            KnowledgeNode.entity_type == "invoice",
            KnowledgeNode.entity_id == invoice_id,
        )
    )
    node_ids = list(result.scalars().all())
    if not node_ids:
        return 0

    await db.execute(
        sa_delete(KnowledgeEdge).where(
            KnowledgeEdge.source_node_id.in_(node_ids)
            | KnowledgeEdge.target_node_id.in_(node_ids)
        )
    )
    await db.execute(sa_delete(KnowledgeNode).where(KnowledgeNode.id.in_(node_ids)))
    await db.flush()
    return len(node_ids)


async def link_feature_to_tool_graph(
    feature_id: uuid.UUID,
    catalog_entry_id: uuid.UUID,
    db: AsyncSession,
) -> dict:
    """
    Add edge: drawing_feature → tool_catalog_entry (machined_with).
    """
    feature_node_result = await db.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.entity_type == "drawing_feature",
            KnowledgeNode.entity_id == feature_id,
        )
    )
    feature_node = feature_node_result.scalar_one_or_none()

    tool_node_result = await db.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.entity_type == "tool_catalog_entry",
            KnowledgeNode.entity_id == catalog_entry_id,
        )
    )
    tool_node = tool_node_result.scalar_one_or_none()

    if not feature_node or not tool_node:
        logger.warning(
            "link_feature_to_tool_nodes_missing",
            feature_node=feature_node is not None,
            tool_node=tool_node is not None,
        )
        return {"linked": False}

    await _upsert_edge(
        db=db,
        source_id=feature_node.id,
        target_id=tool_node.id,
        edge_type="machined_with",
        reason="Элемент чертежа обрабатывается данным инструментом",
    )
    await db.flush()
    return {"linked": True}


async def _upsert_node(
    db: AsyncSession,
    *,
    node_type: str,
    title: str,
    canonical_key: str,
    entity_type: str,
    entity_id: uuid.UUID,
    summary: str | None = None,
    confidence: float = 1.0,
    source_document_id: uuid.UUID | None = None,
) -> KnowledgeNode:
    """Get or create a KnowledgeNode."""
    result = await db.execute(
        select(KnowledgeNode).where(KnowledgeNode.canonical_key == canonical_key)
    )
    node = result.scalar_one_or_none()

    if node:
        node.title = title
        node.summary = summary
        node.confidence = confidence
    else:
        node = KnowledgeNode(
            node_type=node_type,
            title=title,
            canonical_key=canonical_key,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            confidence=confidence,
            created_by="sveta",
            source_document_id=source_document_id,
        )
        db.add(node)
        await db.flush()

    return node


async def _upsert_edge(
    db: AsyncSession,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    edge_type: str,
    reason: str | None = None,
    confidence: float = 1.0,
) -> KnowledgeEdge:
    """Get or create a KnowledgeEdge."""
    result = await db.execute(
        select(KnowledgeEdge).where(
            KnowledgeEdge.source_node_id == source_id,
            KnowledgeEdge.target_node_id == target_id,
            KnowledgeEdge.edge_type == edge_type,
        )
    )
    edge = result.scalar_one_or_none()

    if not edge:
        edge = KnowledgeEdge(
            source_node_id=source_id,
            target_node_id=target_id,
            edge_type=edge_type,
            confidence=confidence,
            reason=reason,
            created_by="sveta",
        )
        db.add(edge)
        await db.flush()

    return edge


async def _get_or_create_document_node(
    db: AsyncSession, document_id: uuid.UUID
) -> KnowledgeNode | None:
    """Find existing document node or skip."""
    result = await db.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.entity_type == "document",
            KnowledgeNode.entity_id == document_id,
        )
    )
    return result.scalar_one_or_none()
