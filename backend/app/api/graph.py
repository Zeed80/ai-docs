"""Knowledge graph API — skills: graph.node_create, graph.edge_create,
graph.neighborhood, graph.path, graph.chunk_create, graph.evidence_create,
graph.mention_create."""

import uuid
from collections import deque
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_action
from app.db.models import (
    Document,
    DocumentChunk,
    EntityMention,
    EvidenceSpan,
    GraphReviewItem,
    KnowledgeEdge,
    KnowledgeNode,
)
from app.db.session import get_db
from app.domain.graph import (
    DocumentChunkCreate,
    DocumentChunkOut,
    EntityMentionCreate,
    EntityMentionOut,
    EvidenceSpanCreate,
    EvidenceSpanOut,
    GraphReviewDecision,
    GraphReviewItemOut,
    GraphReviewListResponse,
    GraphNeighborhoodResponse,
    GraphPathResponse,
    KnowledgeEdgeCreate,
    KnowledgeEdgeOut,
    KnowledgeNodeCreate,
    KnowledgeNodeOut,
)

router = APIRouter()
logger = structlog.get_logger()


@router.post("/nodes", response_model=KnowledgeNodeOut, status_code=201)
async def create_node(
    payload: KnowledgeNodeCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: graph.node_create — Create a graph memory node."""
    existing = None
    if payload.entity_type and payload.entity_id:
        result = await db.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.entity_type == payload.entity_type,
                KnowledgeNode.entity_id == payload.entity_id,
            )
        )
        existing = result.scalar_one_or_none()
    if existing:
        return existing

    node = KnowledgeNode(**payload.model_dump(by_alias=False))
    db.add(node)
    await db.flush()
    await log_action(
        db,
        action="graph.node_create",
        entity_type="knowledge_node",
        entity_id=node.id,
        details={"node_type": node.node_type, "title": node.title},
    )
    await db.commit()
    await db.refresh(node)
    return node


@router.get("/nodes/{node_id}", response_model=KnowledgeNodeOut)
async def get_node(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Skill: graph.node_get — Get a graph memory node."""
    node = await db.get(KnowledgeNode, node_id)
    if not node:
        raise HTTPException(404, "Knowledge node not found")
    return node


@router.post("/edges", response_model=KnowledgeEdgeOut, status_code=201)
async def create_edge(
    payload: KnowledgeEdgeCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: graph.edge_create — Link two graph memory nodes."""
    source = await db.get(KnowledgeNode, payload.source_node_id)
    target = await db.get(KnowledgeNode, payload.target_node_id)
    if not source or not target:
        raise HTTPException(404, "Source or target node not found")
    if payload.source_document_id and not await db.get(Document, payload.source_document_id):
        raise HTTPException(404, "Source document not found")
    if payload.evidence_span_id and not await db.get(EvidenceSpan, payload.evidence_span_id):
        raise HTTPException(404, "Evidence span not found")

    edge = KnowledgeEdge(**payload.model_dump(by_alias=False))
    db.add(edge)
    await db.flush()
    await log_action(
        db,
        action="graph.edge_create",
        entity_type="knowledge_edge",
        entity_id=edge.id,
        details={
            "edge_type": edge.edge_type,
            "source_node_id": str(edge.source_node_id),
            "target_node_id": str(edge.target_node_id),
        },
    )
    await db.commit()
    await db.refresh(edge)
    return edge


@router.get("/nodes/{node_id}/neighborhood", response_model=GraphNeighborhoodResponse)
async def get_neighborhood(
    node_id: uuid.UUID,
    depth: int = Query(1, ge=1, le=3),
    edge_type: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Skill: graph.neighborhood — Get connected graph memory around a node."""
    center = await db.get(KnowledgeNode, node_id)
    if not center:
        raise HTTPException(404, "Knowledge node not found")

    node_ids = {node_id}
    edge_ids: set[uuid.UUID] = set()
    frontier = {node_id}

    for _ in range(depth):
        query = select(KnowledgeEdge).where(
            (KnowledgeEdge.source_node_id.in_(frontier))
            | (KnowledgeEdge.target_node_id.in_(frontier))
        )
        if edge_type:
            query = query.where(KnowledgeEdge.edge_type == edge_type)
        result = await db.execute(query)
        edges = result.scalars().all()
        next_frontier: set[uuid.UUID] = set()
        for edge in edges:
            edge_ids.add(edge.id)
            for nid in (edge.source_node_id, edge.target_node_id):
                if nid not in node_ids:
                    node_ids.add(nid)
                    next_frontier.add(nid)
        if not next_frontier:
            break
        frontier = next_frontier

    nodes_result = await db.execute(select(KnowledgeNode).where(KnowledgeNode.id.in_(node_ids)))
    edges_result = await db.execute(select(KnowledgeEdge).where(KnowledgeEdge.id.in_(edge_ids)))
    return GraphNeighborhoodResponse(
        center=center,
        nodes=list(nodes_result.scalars().all()),
        edges=list(edges_result.scalars().all()),
    )


@router.get("/path", response_model=GraphPathResponse)
async def find_path(
    source_node_id: uuid.UUID,
    target_node_id: uuid.UUID,
    max_depth: int = Query(4, ge=1, le=8),
    db: AsyncSession = Depends(get_db),
):
    """Skill: graph.path — Find a short relationship path between two nodes."""
    source = await db.get(KnowledgeNode, source_node_id)
    target = await db.get(KnowledgeNode, target_node_id)
    if not source or not target:
        raise HTTPException(404, "Source or target node not found")

    queue = deque([(source_node_id, [], [source_node_id])])
    visited = {source_node_id}
    found_edge_ids: list[uuid.UUID] = []
    found_node_ids: list[uuid.UUID] = []

    while queue:
        current_id, edge_path, node_path = queue.popleft()
        if len(edge_path) >= max_depth:
            continue
        result = await db.execute(
            select(KnowledgeEdge).where(
                (KnowledgeEdge.source_node_id == current_id)
                | (KnowledgeEdge.target_node_id == current_id)
            )
        )
        for edge in result.scalars().all():
            neighbor_id = (
                edge.target_node_id if edge.source_node_id == current_id else edge.source_node_id
            )
            if neighbor_id in visited:
                continue
            next_edges = [*edge_path, edge.id]
            next_nodes = [*node_path, neighbor_id]
            if neighbor_id == target_node_id:
                found_edge_ids = next_edges
                found_node_ids = next_nodes
                queue.clear()
                break
            visited.add(neighbor_id)
            queue.append((neighbor_id, next_edges, next_nodes))

    if not found_node_ids:
        return GraphPathResponse(
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            found=False,
        )

    nodes_result = await db.execute(
        select(KnowledgeNode).where(KnowledgeNode.id.in_(found_node_ids))
    )
    edges_result = await db.execute(
        select(KnowledgeEdge).where(KnowledgeEdge.id.in_(found_edge_ids))
    )
    nodes_by_id = {node.id: node for node in nodes_result.scalars().all()}
    edges_by_id = {edge.id: edge for edge in edges_result.scalars().all()}
    return GraphPathResponse(
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        found=True,
        nodes=[nodes_by_id[nid] for nid in found_node_ids if nid in nodes_by_id],
        edges=[edges_by_id[eid] for eid in found_edge_ids if eid in edges_by_id],
    )


@router.post("/chunks", response_model=DocumentChunkOut, status_code=201)
async def create_chunk(
    payload: DocumentChunkCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: graph.chunk_create — Create a memory chunk for a document."""
    if not await db.get(Document, payload.document_id):
        raise HTTPException(404, "Document not found")
    chunk = DocumentChunk(**payload.model_dump(by_alias=False))
    db.add(chunk)
    await db.commit()
    await db.refresh(chunk)
    return chunk


@router.post("/evidence", response_model=EvidenceSpanOut, status_code=201)
async def create_evidence(
    payload: EvidenceSpanCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: graph.evidence_create — Create source evidence span."""
    if not await db.get(Document, payload.document_id):
        raise HTTPException(404, "Document not found")
    if payload.chunk_id and not await db.get(DocumentChunk, payload.chunk_id):
        raise HTTPException(404, "Document chunk not found")
    evidence = EvidenceSpan(**payload.model_dump(by_alias=False))
    db.add(evidence)
    await db.commit()
    await db.refresh(evidence)
    return evidence


@router.post("/mentions", response_model=EntityMentionOut, status_code=201)
async def create_mention(
    payload: EntityMentionCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: graph.mention_create — Create a document entity mention."""
    if not await db.get(Document, payload.document_id):
        raise HTTPException(404, "Document not found")
    if payload.chunk_id and not await db.get(DocumentChunk, payload.chunk_id):
        raise HTTPException(404, "Document chunk not found")
    if payload.node_id and not await db.get(KnowledgeNode, payload.node_id):
        raise HTTPException(404, "Knowledge node not found")
    if payload.evidence_span_id and not await db.get(EvidenceSpan, payload.evidence_span_id):
        raise HTTPException(404, "Evidence span not found")
    mention = EntityMention(**payload.model_dump(by_alias=False))
    db.add(mention)
    await db.commit()
    await db.refresh(mention)
    return mention


@router.get("/review", response_model=GraphReviewListResponse)
async def list_review_items(
    status: str = "pending",
    document_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Skill: graph.review_list — List graph memory links that need review."""
    query = select(GraphReviewItem).where(GraphReviewItem.status == status)
    if document_id:
        query = query.where(GraphReviewItem.document_id == document_id)
    result = await db.execute(
        query.order_by(GraphReviewItem.created_at.desc()).limit(limit)
    )
    items = list(result.scalars().all())
    return GraphReviewListResponse(items=items, total=len(items))


@router.post("/review/{item_id}/decide", response_model=GraphReviewItemOut)
async def decide_review_item(
    item_id: uuid.UUID,
    payload: GraphReviewDecision,
    db: AsyncSession = Depends(get_db),
):
    """Skill: graph.review_decide — Approve or reject a graph memory suggestion."""
    item = await db.get(GraphReviewItem, item_id)
    if not item:
        raise HTTPException(404, "Graph review item not found")
    if item.status != "pending":
        raise HTTPException(400, "Graph review item already decided")

    item.status = "approved" if payload.action == "approve" else "rejected"
    item.decided_by = payload.decided_by
    item.decided_at = datetime.now(UTC)
    item.decision_comment = payload.comment

    if item.edge_id:
        edge = await db.get(KnowledgeEdge, item.edge_id)
        if edge:
            metadata = dict(edge.metadata_ or {})
            metadata["review_status"] = item.status
            if payload.comment:
                metadata["review_comment"] = payload.comment
            edge.metadata_ = metadata
            if payload.action == "reject":
                edge.confidence = 0.0

    await log_action(
        db,
        action="graph.review_decide",
        entity_type="graph_review_item",
        entity_id=item.id,
        details={"status": item.status, "item_type": item.item_type},
    )
    await db.commit()
    await db.refresh(item)
    return item
