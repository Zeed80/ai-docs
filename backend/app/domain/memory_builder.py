"""Deterministic graph-memory builder for documents.

This is the first, conservative memory layer: it creates stable document nodes,
text chunks, source evidence, and obvious entity mentions without relying on an
LLM. AI-assisted linking can be added on top of these facts later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.models import (
    AnomalyCard,
    Approval,
    Document,
    DocumentChunk,
    DocumentVersion,
    EntityMention,
    EvidenceSpan,
    GraphReviewItem,
    Invoice,
    KnowledgeEdge,
    KnowledgeNode,
    Party,
)

CHUNK_SIZE = 3000
CHUNK_OVERLAP = 300
AUTO_METHOD = "deterministic_regex"
REVIEW_THRESHOLD = 0.85
CONFLICT_ENTITY_TYPES = {"material", "invoice"}
GENERATED_EDGE_TYPES = {
    "mentions",
    "conflicts_with",
    "specifies_material",
    "requires",
    "uses_machine",
    "uses_tool",
    "uses_fixture",
    "has_operation",
}
COMPACT_ENTITY_TYPES = {"invoice", "standard"}
EXTENDED_SCOPE_HINTS = re.compile(
    r"\b(?:черт[её]ж|техпроцесс|маршрут|операц\w*|переход|станок|резец|фреза|"
    r"оснастка|приспособление|ГОСТ|ОСТ|ТУ|ЕСТД|ЕСКД)\b",
    re.IGNORECASE,
)

MENTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "material": re.compile(
        r"\b(?:сталь|алюминий|латунь|бронза|чугун)\s+[А-ЯA-Z0-9Хх\-]+",
        re.IGNORECASE,
    ),
    "standard": re.compile(
        r"\b(?:ГОСТ|ОСТ|ТУ|DIN|ISO)\s*[\d.\-–—/]+",
        re.IGNORECASE,
    ),
    "machine": re.compile(
        r"\b(?:станок|центр|токарн\w*|фрезерн\w*|шлифовальн\w*)\s+[A-Za-zА-Яа-я0-9\-]+",
        re.IGNORECASE,
    ),
    "tool": re.compile(
        r"\b(?:сверло|фреза|резец|метчик|развертка|пластина)\s+[A-Za-zА-Яа-я0-9\-.,]+",
        re.IGNORECASE,
    ),
    "fixture": re.compile(
        r"\b(?:оснастка|приспособление|патрон|тиски|кондуктор|оправка)\s+[A-Za-zА-Яа-я0-9\-.,]+",
        re.IGNORECASE,
    ),
    "operation": re.compile(
        r"\b(?:операц(?:ия|ии)|переход|точение|фрезерование|сверление|шлифование)"
        r"\s*(?:№|N)?\s*[A-Za-zА-Яа-я0-9\-]*",
        re.IGNORECASE,
    ),
    "invoice": re.compile(
        r"\b(?:сч[её]т|invoice)\s*(?:№|N|#)?\s*[A-Za-zА-Яа-я0-9\-_/]+",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True)
class MemoryBuildResult:
    document_node_id: str
    chunks_created: int
    evidence_created: int
    mentions_created: int
    edges_created: int
    review_items_created: int


async def build_document_memory_async(
    db: AsyncSession,
    document: Document,
    *,
    text: str | None = None,
    actor: str = "system",
    rebuild: bool = False,
    build_scope: str = "auto",
    context_meta: dict | None = None,
) -> MemoryBuildResult:
    if rebuild:
        await clear_document_memory_async(db, document)

    version_id = await _latest_document_version_id_async(db, document)
    actual_scope = determine_graph_build_scope(document, text, requested_scope=build_scope)
    node = await _get_or_create_document_node_async(db, document, actor=actor)
    node.source_document_id = document.id
    node.source_document_version_id = version_id
    node.metadata_ = {**(node.metadata_ or {}), "build_scope": actual_scope}
    chunks_created = evidence_created = mentions_created = edges_created = review_items_created = 0
    document_entities: dict[str, dict[str, KnowledgeNode]] = {}

    chunks = _split_chunks(text or document.file_name)
    context_prefix = build_chunk_context(document, text=text, extra_meta=context_meta)
    for index, chunk_text in enumerate(chunks):
        chunk = DocumentChunk(
            document_id=document.id,
            document_version_id=version_id,
            chunk_index=index,
            text=chunk_text,
            context_prefix=context_prefix,
            token_count=_rough_token_count(chunk_text),
            metadata_={"method": AUTO_METHOD},
        )
        db.add(chunk)
        await db.flush()
        chunks_created += 1

        mentions = list(_extract_mentions(chunk_text, build_scope=actual_scope))
        if mentions:
            evidence = EvidenceSpan(
                document_id=document.id,
                document_version_id=version_id,
                chunk_id=chunk.id,
                field_name="auto_mentions",
                text=chunk_text[:1000],
                confidence=0.7,
                metadata_={"method": AUTO_METHOD, "build_scope": actual_scope},
            )
            db.add(evidence)
            await db.flush()
            evidence_created += 1
        else:
            evidence = None

        for mention_text, entity_type, start, end in mentions:
            target = await _get_or_create_entity_node_async(
                db, entity_type=entity_type, title=mention_text, actor=actor
            )
            document_entities.setdefault(entity_type, {})[str(target.id)] = target
            mention = EntityMention(
                document_id=document.id,
                document_version_id=version_id,
                chunk_id=chunk.id,
                node_id=target.id,
                mention_text=mention_text,
                entity_type=entity_type,
                start_offset=start,
                end_offset=end,
                confidence=0.72,
                extraction_method=AUTO_METHOD,
                evidence_span_id=evidence.id if evidence else None,
                metadata_={"method": AUTO_METHOD, "build_scope": actual_scope},
            )
            db.add(mention)
            await db.flush()
            mentions_created += 1

            edge_type = _edge_type_for_entity(entity_type, actual_scope)
            edge = KnowledgeEdge(
                source_node_id=node.id,
                target_node_id=target.id,
                edge_type=edge_type,
                confidence=0.72,
                reason=f"Detected {entity_type} mention in document text",
                source_document_id=document.id,
                source_document_version_id=version_id,
                evidence_span_id=evidence.id if evidence else None,
                created_by=actor,
                metadata_={"method": AUTO_METHOD, "build_scope": actual_scope},
            )
            db.add(edge)
            await db.flush()
            edges_created += 1
            if edge.confidence < REVIEW_THRESHOLD:
                db.add(
                    GraphReviewItem(
                        item_type="edge",
                        status="pending",
                        document_id=document.id,
                        node_id=target.id,
                        edge_id=edge.id,
                        mention_id=mention.id,
                        confidence=edge.confidence,
                        reason=edge.reason,
                        suggested_by=actor,
                        metadata_={"method": AUTO_METHOD, "review_threshold": REVIEW_THRESHOLD},
                    )
                )
                review_items_created += 1

    conflict_edges, conflict_reviews = await _create_conflict_edges_async(
        db,
        document=document,
        document_version_id=version_id,
        entities_by_type=document_entities,
        actor=actor,
    )
    edges_created += conflict_edges
    review_items_created += conflict_reviews

    return MemoryBuildResult(
        document_node_id=str(node.id),
        chunks_created=chunks_created,
        evidence_created=evidence_created,
        mentions_created=mentions_created,
        edges_created=edges_created,
        review_items_created=review_items_created,
    )


def build_document_memory_sync(
    db: Session,
    document: Document,
    *,
    text: str | None = None,
    actor: str = "system",
    rebuild: bool = False,
    build_scope: str = "auto",
    context_meta: dict | None = None,
) -> MemoryBuildResult:
    if rebuild:
        clear_document_memory_sync(db, document)

    version_id = _latest_document_version_id_sync(db, document)
    actual_scope = determine_graph_build_scope(document, text, requested_scope=build_scope)
    node = _get_or_create_document_node_sync(db, document, actor=actor)
    node.source_document_id = document.id
    node.source_document_version_id = version_id
    node.metadata_ = {**(node.metadata_ or {}), "build_scope": actual_scope}
    chunks_created = evidence_created = mentions_created = edges_created = review_items_created = 0
    document_entities: dict[str, dict[str, KnowledgeNode]] = {}

    chunks = _split_chunks(text or document.file_name)
    context_prefix = build_chunk_context(document, text=text, extra_meta=context_meta)
    for index, chunk_text in enumerate(chunks):
        chunk = DocumentChunk(
            document_id=document.id,
            document_version_id=version_id,
            chunk_index=index,
            text=chunk_text,
            context_prefix=context_prefix,
            token_count=_rough_token_count(chunk_text),
            metadata_={"method": AUTO_METHOD},
        )
        db.add(chunk)
        db.flush()
        chunks_created += 1

        mentions = list(_extract_mentions(chunk_text, build_scope=actual_scope))
        if mentions:
            evidence = EvidenceSpan(
                document_id=document.id,
                document_version_id=version_id,
                chunk_id=chunk.id,
                field_name="auto_mentions",
                text=chunk_text[:1000],
                confidence=0.7,
                metadata_={"method": AUTO_METHOD, "build_scope": actual_scope},
            )
            db.add(evidence)
            db.flush()
            evidence_created += 1
        else:
            evidence = None

        for mention_text, entity_type, start, end in mentions:
            target = _get_or_create_entity_node_sync(
                db, entity_type=entity_type, title=mention_text, actor=actor
            )
            document_entities.setdefault(entity_type, {})[str(target.id)] = target
            mention = EntityMention(
                document_id=document.id,
                document_version_id=version_id,
                chunk_id=chunk.id,
                node_id=target.id,
                mention_text=mention_text,
                entity_type=entity_type,
                start_offset=start,
                end_offset=end,
                confidence=0.72,
                extraction_method=AUTO_METHOD,
                evidence_span_id=evidence.id if evidence else None,
                metadata_={"method": AUTO_METHOD, "build_scope": actual_scope},
            )
            db.add(mention)
            db.flush()
            mentions_created += 1
            edge_type = _edge_type_for_entity(entity_type, actual_scope)
            edge = KnowledgeEdge(
                source_node_id=node.id,
                target_node_id=target.id,
                edge_type=edge_type,
                confidence=0.72,
                reason=f"Detected {entity_type} mention in document text",
                source_document_id=document.id,
                source_document_version_id=version_id,
                evidence_span_id=evidence.id if evidence else None,
                created_by=actor,
                metadata_={"method": AUTO_METHOD, "build_scope": actual_scope},
            )
            db.add(edge)
            db.flush()
            edges_created += 1
            if edge.confidence < REVIEW_THRESHOLD:
                db.add(
                    GraphReviewItem(
                        item_type="edge",
                        status="pending",
                        document_id=document.id,
                        node_id=target.id,
                        edge_id=edge.id,
                        mention_id=mention.id,
                        confidence=edge.confidence,
                        reason=edge.reason,
                        suggested_by=actor,
                        metadata_={"method": AUTO_METHOD, "review_threshold": REVIEW_THRESHOLD},
                    )
                )
                review_items_created += 1

    conflict_edges, conflict_reviews = _create_conflict_edges_sync(
        db,
        document=document,
        document_version_id=version_id,
        entities_by_type=document_entities,
        actor=actor,
    )
    edges_created += conflict_edges
    review_items_created += conflict_reviews

    return MemoryBuildResult(
        document_node_id=str(node.id),
        chunks_created=chunks_created,
        evidence_created=evidence_created,
        mentions_created=mentions_created,
        edges_created=edges_created,
        review_items_created=review_items_created,
    )


async def clear_document_memory_async(db: AsyncSession, document: Document) -> None:
    review_result = await db.execute(
        select(GraphReviewItem).where(
            GraphReviewItem.document_id == document.id,
            GraphReviewItem.suggested_by == "system",
        )
    )
    for item in review_result.scalars().all():
        await db.delete(item)

    mention_result = await db.execute(
        select(EntityMention).where(
            EntityMention.document_id == document.id,
            EntityMention.extraction_method == AUTO_METHOD,
        )
    )
    for mention in mention_result.scalars().all():
        await db.delete(mention)

    edge_result = await db.execute(
        select(KnowledgeEdge).where(
            KnowledgeEdge.source_document_id == document.id,
            KnowledgeEdge.created_by == "system",
            KnowledgeEdge.edge_type.in_(GENERATED_EDGE_TYPES),
        )
    )
    for edge in edge_result.scalars().all():
        await db.delete(edge)

    evidence_result = await db.execute(
        select(EvidenceSpan).where(
            EvidenceSpan.document_id == document.id,
            EvidenceSpan.field_name == "auto_mentions",
        )
    )
    for evidence in evidence_result.scalars().all():
        await db.delete(evidence)

    chunk_result = await db.execute(
        select(DocumentChunk).where(DocumentChunk.document_id == document.id)
    )
    for chunk in chunk_result.scalars().all():
        if (chunk.metadata_ or {}).get("method") == AUTO_METHOD:
            await db.delete(chunk)

    await db.flush()


def clear_document_memory_sync(db: Session, document: Document) -> None:
    for item in db.execute(
        select(GraphReviewItem).where(
            GraphReviewItem.document_id == document.id,
            GraphReviewItem.suggested_by == "system",
        )
    ).scalars().all():
        db.delete(item)

    for mention in db.execute(
        select(EntityMention).where(
            EntityMention.document_id == document.id,
            EntityMention.extraction_method == AUTO_METHOD,
        )
    ).scalars().all():
        db.delete(mention)

    for edge in db.execute(
        select(KnowledgeEdge).where(
            KnowledgeEdge.source_document_id == document.id,
            KnowledgeEdge.created_by == "system",
            KnowledgeEdge.edge_type.in_(GENERATED_EDGE_TYPES),
        )
    ).scalars().all():
        db.delete(edge)

    for evidence in db.execute(
        select(EvidenceSpan).where(
            EvidenceSpan.document_id == document.id,
            EvidenceSpan.field_name == "auto_mentions",
        )
    ).scalars().all():
        db.delete(evidence)

    for chunk in db.execute(
        select(DocumentChunk).where(DocumentChunk.document_id == document.id)
    ).scalars().all():
        if (chunk.metadata_ or {}).get("method") == AUTO_METHOD:
            db.delete(chunk)

    db.flush()


async def _latest_document_version_id_async(db: AsyncSession, document: Document):
    result = await db.execute(
        select(DocumentVersion.id)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.version_number.desc(), DocumentVersion.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _latest_document_version_id_sync(db: Session, document: Document):
    return db.execute(
        select(DocumentVersion.id)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.version_number.desc(), DocumentVersion.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


async def _create_conflict_edges_async(
    db: AsyncSession,
    *,
    document: Document,
    document_version_id,
    entities_by_type: dict[str, dict[str, KnowledgeNode]],
    actor: str,
) -> tuple[int, int]:
    edges_created = review_items_created = 0
    for entity_type, nodes_by_id in entities_by_type.items():
        if entity_type not in CONFLICT_ENTITY_TYPES or len(nodes_by_id) < 2:
            continue
        nodes = sorted(nodes_by_id.values(), key=lambda node: node.title.lower())
        for index, source in enumerate(nodes[:-1]):
            for target in nodes[index + 1 :]:
                edge = KnowledgeEdge(
                    source_node_id=source.id,
                    target_node_id=target.id,
                    edge_type="conflicts_with",
                    confidence=0.6,
                    reason=(
                        f"Multiple distinct {entity_type} values were detected in one document"
                    ),
                    source_document_id=document.id,
                    source_document_version_id=document_version_id,
                    created_by=actor,
                    metadata_={"method": AUTO_METHOD, "conflict_entity_type": entity_type},
                )
                db.add(edge)
                await db.flush()
                edges_created += 1
                db.add(
                    GraphReviewItem(
                        item_type="edge",
                        status="pending",
                        document_id=document.id,
                        node_id=target.id,
                        edge_id=edge.id,
                        confidence=edge.confidence,
                        reason=edge.reason,
                        suggested_by=actor,
                        metadata_={
                            "method": AUTO_METHOD,
                            "review_threshold": REVIEW_THRESHOLD,
                            "conflict_entity_type": entity_type,
                        },
                    )
                )
                review_items_created += 1
    return edges_created, review_items_created


def _create_conflict_edges_sync(
    db: Session,
    *,
    document: Document,
    document_version_id,
    entities_by_type: dict[str, dict[str, KnowledgeNode]],
    actor: str,
) -> tuple[int, int]:
    edges_created = review_items_created = 0
    for entity_type, nodes_by_id in entities_by_type.items():
        if entity_type not in CONFLICT_ENTITY_TYPES or len(nodes_by_id) < 2:
            continue
        nodes = sorted(nodes_by_id.values(), key=lambda node: node.title.lower())
        for index, source in enumerate(nodes[:-1]):
            for target in nodes[index + 1 :]:
                edge = KnowledgeEdge(
                    source_node_id=source.id,
                    target_node_id=target.id,
                    edge_type="conflicts_with",
                    confidence=0.6,
                    reason=(
                        f"Multiple distinct {entity_type} values were detected in one document"
                    ),
                    source_document_id=document.id,
                    source_document_version_id=document_version_id,
                    created_by=actor,
                    metadata_={"method": AUTO_METHOD, "conflict_entity_type": entity_type},
                )
                db.add(edge)
                db.flush()
                edges_created += 1
                db.add(
                    GraphReviewItem(
                        item_type="edge",
                        status="pending",
                        document_id=document.id,
                        node_id=target.id,
                        edge_id=edge.id,
                        confidence=edge.confidence,
                        reason=edge.reason,
                        suggested_by=actor,
                        metadata_={
                            "method": AUTO_METHOD,
                            "review_threshold": REVIEW_THRESHOLD,
                            "conflict_entity_type": entity_type,
                        },
                    )
                )
                review_items_created += 1
    return edges_created, review_items_created


async def _get_or_create_document_node_async(
    db: AsyncSession,
    document: Document,
    *,
    actor: str,
) -> KnowledgeNode:
    result = await db.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.entity_type == "document",
            KnowledgeNode.entity_id == document.id,
        )
    )
    node = result.scalar_one_or_none()
    if node:
        return node
    node = KnowledgeNode(
        node_type="document",
        title=document.file_name,
        canonical_key=f"document:{document.file_hash}",
        entity_type="document",
        entity_id=document.id,
        summary=f"Document {document.file_name}",
        confidence=1.0,
        created_by=actor,
        metadata_={"mime_type": document.mime_type, "source_channel": document.source_channel},
    )
    db.add(node)
    await db.flush()
    return node


def _get_or_create_document_node_sync(
    db: Session,
    document: Document,
    *,
    actor: str,
) -> KnowledgeNode:
    node = db.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.entity_type == "document",
            KnowledgeNode.entity_id == document.id,
        )
    ).scalar_one_or_none()
    if node:
        return node
    node = KnowledgeNode(
        node_type="document",
        title=document.file_name,
        canonical_key=f"document:{document.file_hash}",
        entity_type="document",
        entity_id=document.id,
        summary=f"Document {document.file_name}",
        confidence=1.0,
        created_by=actor,
        metadata_={"mime_type": document.mime_type, "source_channel": document.source_channel},
    )
    db.add(node)
    db.flush()
    return node


async def _get_or_create_entity_node_async(
    db: AsyncSession,
    *,
    entity_type: str,
    title: str,
    actor: str,
) -> KnowledgeNode:
    canonical_key = f"{entity_type}:{_normalize_key(title)}"
    result = await db.execute(
        select(KnowledgeNode).where(KnowledgeNode.canonical_key == canonical_key)
    )
    node = result.scalar_one_or_none()
    if node:
        return node
    node = KnowledgeNode(
        node_type=entity_type,
        title=title,
        canonical_key=canonical_key,
        summary=f"Auto-detected {entity_type}: {title}",
        confidence=0.72,
        created_by=actor,
        metadata_={"method": "deterministic_regex"},
    )
    db.add(node)
    await db.flush()
    return node


def _get_or_create_entity_node_sync(
    db: Session,
    *,
    entity_type: str,
    title: str,
    actor: str,
) -> KnowledgeNode:
    canonical_key = f"{entity_type}:{_normalize_key(title)}"
    node = db.execute(
        select(KnowledgeNode).where(KnowledgeNode.canonical_key == canonical_key)
    ).scalar_one_or_none()
    if node:
        return node
    node = KnowledgeNode(
        node_type=entity_type,
        title=title,
        canonical_key=canonical_key,
        summary=f"Auto-detected {entity_type}: {title}",
        confidence=0.72,
        created_by=actor,
        metadata_={"method": "deterministic_regex"},
    )
    db.add(node)
    db.flush()
    return node


# ── Business-entity graph (supplier → invoice → anomaly → approval) ────────
#
# Unlike the text-mention nodes above (material/standard/tool/...), these are
# anchored on the real business row (entity_type/entity_id), not on a regex
# match — so the graph carries actual supplier/invoice/anomaly/approval
# relationships, not just document content. Each builder is a plain upsert:
# safe to call again on correction/status-change, never duplicates a node or
# edge. Supplier nodes deliberately carry no source_document_id — they must
# outlive the deletion of any single invoice/document (see hard_delete_document
# cascade in app/domain/document_deletion.py, which keys off source_document_id).
BUSINESS_EDGE_TYPES = {"has_invoice", "has_anomaly", "leads_to_approval"}


async def _get_or_create_business_node_async(
    db: AsyncSession,
    *,
    node_type: str,
    entity_type: str,
    entity_id,
    title: str,
    summary: str | None = None,
    actor: str = "system",
    confidence: float = 1.0,
    metadata: dict | None = None,
    source_document_id=None,
) -> KnowledgeNode:
    result = await db.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.entity_type == entity_type,
            KnowledgeNode.entity_id == entity_id,
        )
    )
    node = result.scalar_one_or_none()
    if node is None:
        node = KnowledgeNode(
            node_type=node_type,
            entity_type=entity_type,
            entity_id=entity_id,
            canonical_key=f"{entity_type}:{entity_id}",
            created_by=actor,
        )
        db.add(node)
    # Re-running a builder (correction, status change) refreshes the existing
    # row in place instead of creating a duplicate — keeps the node current.
    node.title = title
    node.summary = summary
    node.confidence = confidence
    node.metadata_ = {**(node.metadata_ or {}), **(metadata or {})}
    if source_document_id is not None:
        node.source_document_id = source_document_id
    await db.flush()
    return node


def _get_or_create_business_node_sync(
    db: Session,
    *,
    node_type: str,
    entity_type: str,
    entity_id,
    title: str,
    summary: str | None = None,
    actor: str = "system",
    confidence: float = 1.0,
    metadata: dict | None = None,
    source_document_id=None,
) -> KnowledgeNode:
    node = db.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.entity_type == entity_type,
            KnowledgeNode.entity_id == entity_id,
        )
    ).scalar_one_or_none()
    if node is None:
        node = KnowledgeNode(
            node_type=node_type,
            entity_type=entity_type,
            entity_id=entity_id,
            canonical_key=f"{entity_type}:{entity_id}",
            created_by=actor,
        )
        db.add(node)
    node.title = title
    node.summary = summary
    node.confidence = confidence
    node.metadata_ = {**(node.metadata_ or {}), **(metadata or {})}
    if source_document_id is not None:
        node.source_document_id = source_document_id
    db.flush()
    return node


async def _get_or_create_business_edge_async(
    db: AsyncSession,
    *,
    source_node: KnowledgeNode,
    target_node: KnowledgeNode,
    edge_type: str,
    actor: str = "system",
    source_document_id=None,
    reason: str | None = None,
    confidence: float = 1.0,
    metadata: dict | None = None,
) -> KnowledgeEdge:
    result = await db.execute(
        select(KnowledgeEdge).where(
            KnowledgeEdge.source_node_id == source_node.id,
            KnowledgeEdge.target_node_id == target_node.id,
            KnowledgeEdge.edge_type == edge_type,
        )
    )
    edge = result.scalar_one_or_none()
    if edge is None:
        edge = KnowledgeEdge(
            source_node_id=source_node.id,
            target_node_id=target_node.id,
            edge_type=edge_type,
            created_by=actor,
        )
        db.add(edge)
    edge.confidence = confidence
    edge.reason = reason
    if source_document_id is not None:
        edge.source_document_id = source_document_id
    # Status-only changes (anomaly resolved, approval decided) land here as a
    # metadata update on the SAME edge row — history stays in AuditLog, the
    # graph reflects current state without growing duplicate edges per change.
    edge.metadata_ = {**(edge.metadata_ or {}), **(metadata or {})}
    await db.flush()
    return edge


def _get_or_create_business_edge_sync(
    db: Session,
    *,
    source_node: KnowledgeNode,
    target_node: KnowledgeNode,
    edge_type: str,
    actor: str = "system",
    source_document_id=None,
    reason: str | None = None,
    confidence: float = 1.0,
    metadata: dict | None = None,
) -> KnowledgeEdge:
    edge = db.execute(
        select(KnowledgeEdge).where(
            KnowledgeEdge.source_node_id == source_node.id,
            KnowledgeEdge.target_node_id == target_node.id,
            KnowledgeEdge.edge_type == edge_type,
        )
    ).scalar_one_or_none()
    if edge is None:
        edge = KnowledgeEdge(
            source_node_id=source_node.id,
            target_node_id=target_node.id,
            edge_type=edge_type,
            created_by=actor,
        )
        db.add(edge)
    edge.confidence = confidence
    edge.reason = reason
    if source_document_id is not None:
        edge.source_document_id = source_document_id
    edge.metadata_ = {**(edge.metadata_ or {}), **(metadata or {})}
    db.flush()
    return edge


async def _find_node_by_entity_async(db: AsyncSession, *, entity_type: str, entity_id) -> KnowledgeNode | None:
    result = await db.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.entity_type == entity_type,
            KnowledgeNode.entity_id == entity_id,
        )
    )
    return result.scalar_one_or_none()


def _find_node_by_entity_sync(db: Session, *, entity_type: str, entity_id) -> KnowledgeNode | None:
    return db.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.entity_type == entity_type,
            KnowledgeNode.entity_id == entity_id,
        )
    ).scalar_one_or_none()


def _supplier_summary(supplier: Party) -> str:
    parts = [f"Поставщик {supplier.name}"]
    if supplier.inn:
        parts.append(f"ИНН {supplier.inn}")
    return ", ".join(parts)


def _invoice_summary(invoice: Invoice) -> str:
    bits = [f"Счёт №{invoice.invoice_number or invoice.id}"]
    if invoice.total_amount is not None:
        bits.append(f"на {invoice.total_amount} {invoice.currency}")
    if invoice.invoice_date:
        bits.append(f"от {invoice.invoice_date:%Y-%m-%d}")
    return " ".join(bits)


async def build_supplier_invoice_memory_async(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor: str = "system",
) -> tuple[KnowledgeNode | None, KnowledgeNode | None]:
    """Anchor supplier + invoice nodes and link supplier --has_invoice--> invoice.

    Safe to call again after a correction (e.g. supplier reassigned via
    doc.correct_field): the stale edge from the previous supplier is dropped
    before the new one is created, so the invoice never points at two
    suppliers at once.
    """
    if invoice.supplier_id is None:
        return None, None
    # Always query by FK instead of touching invoice.supplier — the relationship
    # may not be eagerly loaded on the passed-in instance, and accessing an
    # unloaded relationship from an AsyncSession context raises MissingGreenlet.
    result = await db.execute(select(Party).where(Party.id == invoice.supplier_id))
    supplier = result.scalar_one_or_none()
    if supplier is None:
        return None, None

    supplier_node = await _get_or_create_business_node_async(
        db,
        node_type="supplier",
        entity_type="supplier",
        entity_id=supplier.id,
        title=supplier.name,
        summary=_supplier_summary(supplier),
        actor=actor,
        metadata={"inn": supplier.inn},
    )
    invoice_node = await _get_or_create_business_node_async(
        db,
        node_type="invoice",
        entity_type="invoice",
        entity_id=invoice.id,
        title=f"Счёт №{invoice.invoice_number or invoice.id}",
        summary=_invoice_summary(invoice),
        actor=actor,
        metadata={
            "status": getattr(invoice.status, "value", invoice.status),
            "total_amount": invoice.total_amount,
        },
        source_document_id=invoice.document_id,
    )

    stale_edges = await db.execute(
        select(KnowledgeEdge).where(
            KnowledgeEdge.target_node_id == invoice_node.id,
            KnowledgeEdge.edge_type == "has_invoice",
            KnowledgeEdge.source_node_id != supplier_node.id,
        )
    )
    for stale in stale_edges.scalars().all():
        await db.delete(stale)

    await _get_or_create_business_edge_async(
        db,
        source_node=supplier_node,
        target_node=invoice_node,
        edge_type="has_invoice",
        actor=actor,
        source_document_id=invoice.document_id,
        reason="Invoice issued by this supplier",
    )
    await db.flush()
    return supplier_node, invoice_node


def build_supplier_invoice_memory_sync(
    db: Session,
    invoice: Invoice,
    *,
    actor: str = "system",
) -> tuple[KnowledgeNode | None, KnowledgeNode | None]:
    if invoice.supplier_id is None:
        return None, None
    supplier = invoice.supplier
    if supplier is None:
        supplier = db.execute(
            select(Party).where(Party.id == invoice.supplier_id)
        ).scalar_one_or_none()
    if supplier is None:
        return None, None

    supplier_node = _get_or_create_business_node_sync(
        db,
        node_type="supplier",
        entity_type="supplier",
        entity_id=supplier.id,
        title=supplier.name,
        summary=_supplier_summary(supplier),
        actor=actor,
        metadata={"inn": supplier.inn},
    )
    invoice_node = _get_or_create_business_node_sync(
        db,
        node_type="invoice",
        entity_type="invoice",
        entity_id=invoice.id,
        title=f"Счёт №{invoice.invoice_number or invoice.id}",
        summary=_invoice_summary(invoice),
        actor=actor,
        metadata={
            "status": getattr(invoice.status, "value", invoice.status),
            "total_amount": invoice.total_amount,
        },
        source_document_id=invoice.document_id,
    )

    stale_edges = db.execute(
        select(KnowledgeEdge).where(
            KnowledgeEdge.target_node_id == invoice_node.id,
            KnowledgeEdge.edge_type == "has_invoice",
            KnowledgeEdge.source_node_id != supplier_node.id,
        )
    ).scalars().all()
    for stale in stale_edges:
        db.delete(stale)

    _get_or_create_business_edge_sync(
        db,
        source_node=supplier_node,
        target_node=invoice_node,
        edge_type="has_invoice",
        actor=actor,
        source_document_id=invoice.document_id,
        reason="Invoice issued by this supplier",
    )
    db.flush()
    return supplier_node, invoice_node


async def build_anomaly_memory_async(
    db: AsyncSession,
    anomaly: AnomalyCard,
    *,
    actor: str = "system",
) -> KnowledgeNode:
    """Anchor an anomaly node and link it from its subject (invoice, etc.).

    Call again on resolve/false_positive — updates the same node + edge
    metadata in place (status, resolved_by); audit history stays in
    AuditLog/timeline, the graph only reflects the current state.
    """
    subject_node = await _find_node_by_entity_async(
        db, entity_type=anomaly.entity_type, entity_id=anomaly.entity_id
    )
    anomaly_node = await _get_or_create_business_node_async(
        db,
        node_type="anomaly",
        entity_type="anomaly",
        entity_id=anomaly.id,
        title=anomaly.title,
        summary=anomaly.description,
        actor=actor,
        metadata={
            "anomaly_type": getattr(anomaly.anomaly_type, "value", anomaly.anomaly_type),
            "severity": getattr(anomaly.severity, "value", anomaly.severity),
            "status": getattr(anomaly.status, "value", anomaly.status),
        },
    )
    if subject_node is not None:
        await _get_or_create_business_edge_async(
            db,
            source_node=subject_node,
            target_node=anomaly_node,
            edge_type="has_anomaly",
            actor=actor,
            reason=f"Anomaly detected: {getattr(anomaly.anomaly_type, 'value', anomaly.anomaly_type)}",
            metadata={"status": getattr(anomaly.status, "value", anomaly.status)},
        )
    await db.flush()
    return anomaly_node


async def build_approval_memory_async(
    db: AsyncSession,
    approval: Approval,
    *,
    actor: str = "system",
) -> KnowledgeNode:
    """Anchor an approval node and link it from its subject (anomaly, invoice, ...).

    Call again on decide_approval — updates the same node + edge metadata
    (status: pending/approved/rejected) instead of creating a new edge per
    decision, so the approval chain stays a single traceable node.
    """
    subject_node = await _find_node_by_entity_async(
        db, entity_type=approval.entity_type, entity_id=approval.entity_id
    )
    approval_node = await _get_or_create_business_node_async(
        db,
        node_type="approval",
        entity_type="approval",
        entity_id=approval.id,
        title=f"Согласование: {getattr(approval.action_type, 'value', approval.action_type)}",
        summary=approval.decision_comment,
        actor=actor,
        metadata={
            "action_type": getattr(approval.action_type, "value", approval.action_type),
            "status": getattr(approval.status, "value", approval.status),
            "decided_by": approval.decided_by,
        },
    )
    if subject_node is not None:
        await _get_or_create_business_edge_async(
            db,
            source_node=subject_node,
            target_node=approval_node,
            edge_type="leads_to_approval",
            actor=actor,
            reason=f"Approval requested: {getattr(approval.action_type, 'value', approval.action_type)}",
            metadata={"status": getattr(approval.status, "value", approval.status)},
        )
    await db.flush()
    return approval_node


async def reconcile_orphaned_business_nodes_async(db: AsyncSession) -> int:
    """Safety-net sweep: drop graph nodes whose source row no longer exists.

    The explicit hooks (build_*_memory_*) keep the graph in sync on the
    happy path, but this catches anything that slipped through — a direct
    SQL delete, a future code path that bypasses the hook. Not the primary
    mechanism; cheap enough to run on a schedule (see AgentCron / celery beat
    ``memory.reconcile_graph``).
    """
    removed = 0
    checks = (
        ("invoice", Invoice),
        ("anomaly", AnomalyCard),
        ("approval", Approval),
    )
    for entity_type, model in checks:
        result = await db.execute(
            select(KnowledgeNode.id, KnowledgeNode.entity_id).where(
                KnowledgeNode.entity_type == entity_type
            )
        )
        rows = result.all()
        if not rows:
            continue
        entity_ids = [row.entity_id for row in rows]
        existing = await db.execute(select(model.id).where(model.id.in_(entity_ids)))
        existing_ids = {row[0] for row in existing.all()}
        orphan_node_ids = [row.id for row in rows if row.entity_id not in existing_ids]
        if not orphan_node_ids:
            continue
        await db.execute(
            KnowledgeEdge.__table__.delete().where(
                KnowledgeEdge.source_node_id.in_(orphan_node_ids)
                | KnowledgeEdge.target_node_id.in_(orphan_node_ids)
            )
        )
        await db.execute(
            KnowledgeNode.__table__.delete().where(KnowledgeNode.id.in_(orphan_node_ids))
        )
        removed += len(orphan_node_ids)
    await db.flush()
    return removed


async def backfill_business_graph_async(
    db: AsyncSession,
    *,
    batch_size: int = 200,
) -> dict[str, int]:
    """Rebuild business-entity graph nodes/edges for every existing row.

    Covers documents approved before the graph-memory hooks (build_*_memory_*)
    existed, or any gap left by a bypassed code path — walks every Invoice
    (with a supplier), AnomalyCard and Approval and re-runs the same
    idempotent builders used on the live hooks, so it's safe to run
    repeatedly (upsert by entity_id, not insert).
    """
    counts = {"invoices": 0, "anomalies": 0, "approvals": 0}

    offset = 0
    while True:
        result = await db.execute(
            select(Invoice)
            .where(Invoice.supplier_id.isnot(None))
            .order_by(Invoice.id)
            .offset(offset)
            .limit(batch_size)
        )
        rows = result.scalars().all()
        if not rows:
            break
        for invoice in rows:
            await build_supplier_invoice_memory_async(db, invoice)
            counts["invoices"] += 1
        await db.flush()
        offset += batch_size

    offset = 0
    while True:
        result = await db.execute(
            select(AnomalyCard).order_by(AnomalyCard.id).offset(offset).limit(batch_size)
        )
        rows = result.scalars().all()
        if not rows:
            break
        for anomaly in rows:
            await build_anomaly_memory_async(db, anomaly)
            counts["anomalies"] += 1
        await db.flush()
        offset += batch_size

    offset = 0
    while True:
        result = await db.execute(
            select(Approval).order_by(Approval.id).offset(offset).limit(batch_size)
        )
        rows = result.scalars().all()
        if not rows:
            break
        for approval in rows:
            await build_approval_memory_async(db, approval)
            counts["approvals"] += 1
        await db.flush()
        offset += batch_size

    return counts


_DOC_TYPE_LABELS = {
    "invoice": "Счёт",
    "letter": "Письмо",
    "contract": "Договор",
    "drawing": "Чертёж",
    "commercial_offer": "Коммерческое предложение",
    "act": "Акт",
    "waybill": "Накладная",
    "other": "Документ",
}

# Metadata keys worth surfacing in the context prefix, in display order.
# Pipelines store extracted fields on Document.metadata_; we read whatever
# is present without requiring any particular extractor to have run.
_CONTEXT_META_FIELDS = (
    ("supplier_name", "поставщик"),
    ("supplier", "поставщик"),
    ("counterparty", "контрагент"),
    ("number", "№"),
    ("invoice_number", "№"),
    ("doc_number", "№"),
    ("date", "от"),
    ("invoice_date", "от"),
    ("total", "сумма"),
    ("total_amount", "сумма"),
    ("subject", "тема"),
)


def build_chunk_context(
    document: Document,
    *,
    text: str | None = None,
    extra_meta: dict | None = None,
) -> str:
    """Build a deterministic context prefix for Contextual Retrieval.

    Each chunk is prefixed with a one-line document descriptor (type, file, and
    extracted key fields) before embedding, so a fragment like "цена 50 шт"
    stays findable as "цена позиций в счёте от ООО Ромашка". This matters for
    chunks past the first: the header chunk carries supplier/number/date in its
    own text, but later chunks lose that anchor without the prefix.

    ``extra_meta`` (extracted fields the caller already has — e.g. invoice
    supplier/number/date) takes priority over ``Document.metadata_``, which on
    most documents holds only pipeline flags. Deterministic by design: no
    per-chunk LLM call, so reindexing thousands of documents of any format
    stays cheap. Returns at least the document-type label.
    """
    doc_type = getattr(document.doc_type, "value", document.doc_type) or "other"
    label = _DOC_TYPE_LABELS.get(str(doc_type), "Документ")
    parts: list[str] = [label]

    meta = {**(document.metadata_ or {}), **(extra_meta or {})}
    seen_labels: set[str] = set()
    for key, display in _CONTEXT_META_FIELDS:
        value = meta.get(key)
        if value in (None, "", []) or display in seen_labels:
            continue
        text_value = " ".join(str(value).split())[:120]
        if not text_value:
            continue
        parts.append(f"№{text_value}" if display == "№" else f"{display} {text_value}")
        seen_labels.add(display)

    if document.file_name and len(parts) == 1:
        # No extracted fields — fall back to the file name for a bit of context.
        parts.append(f"«{document.file_name}»")

    prefix = ", ".join(parts).strip(", ")
    return prefix if len(prefix) > len(label) else label


def _split_chunks(text: str) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if len(cleaned) <= CHUNK_SIZE:
        return [cleaned]
    chunks = []
    start = 0
    while start < len(cleaned):
        end = min(start + CHUNK_SIZE, len(cleaned))
        chunks.append(cleaned[start:end])
        if end >= len(cleaned):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def determine_graph_build_scope(
    document: Document,
    text: str | None = None,
    *,
    requested_scope: str = "auto",
) -> str:
    if requested_scope in {"compact", "extended"}:
        return requested_scope
    doc_type = getattr(document.doc_type, "value", document.doc_type)
    if doc_type == "drawing" or document.source_channel == "ntd":
        return "extended"
    haystack = " ".join([document.file_name or "", text or ""])
    return "extended" if EXTENDED_SCOPE_HINTS.search(haystack) else "compact"


def _extract_mentions(text: str, *, build_scope: str) -> Iterable[tuple[str, str, int, int]]:
    seen: set[tuple[str, str]] = set()
    for entity_type, pattern in MENTION_PATTERNS.items():
        if build_scope == "compact" and entity_type not in COMPACT_ENTITY_TYPES:
            continue
        for match in pattern.finditer(text):
            value = re.sub(r"\s+", " ", match.group(0).strip(" .,;:\n\t"))
            key = (entity_type, value.lower())
            if len(value) < 3 or key in seen:
                continue
            seen.add(key)
            yield value, entity_type, match.start(), match.end()


def _edge_type_for_entity(entity_type: str, build_scope: str) -> str:
    if build_scope != "extended":
        return "mentions"
    return {
        "material": "specifies_material",
        "standard": "requires",
        "machine": "uses_machine",
        "tool": "uses_tool",
        "fixture": "uses_fixture",
        "operation": "has_operation",
    }.get(entity_type, "mentions")


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "-", value.lower()).strip("-")


def _rough_token_count(text: str) -> int:
    return max(1, len(text.split()))
