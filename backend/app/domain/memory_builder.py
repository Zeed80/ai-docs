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
    Document,
    DocumentChunk,
    DocumentVersion,
    EntityMention,
    EvidenceSpan,
    GraphReviewItem,
    KnowledgeEdge,
    KnowledgeNode,
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
    for index, chunk_text in enumerate(chunks):
        chunk = DocumentChunk(
            document_id=document.id,
            document_version_id=version_id,
            chunk_index=index,
            text=chunk_text,
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
    for index, chunk_text in enumerate(chunks):
        chunk = DocumentChunk(
            document_id=document.id,
            document_version_id=version_id,
            chunk_index=index,
            text=chunk_text,
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
