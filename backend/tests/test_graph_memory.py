"""Fast checks for graph memory models and schemas."""

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    Document,
    DocumentChunk,
    DocumentStatus,
    DocumentVersion,
    EntityMention,
    EvidenceSpan,
    GraphReviewItem,
    KnowledgeEdge,
    KnowledgeNode,
    MemoryEmbeddingRecord,
)
from app.api.memory import _merge_memory_hits, _rank_memory_hit
from app.domain.graph import KnowledgeNodeCreate, MemoryExplainResponse, MemorySearchHit, MemorySearchRequest
from app.domain.memory_builder import build_document_memory_sync, determine_graph_build_scope


def test_graph_memory_tables_are_registered() -> None:
    expected = {
        "knowledge_nodes",
        "knowledge_edges",
        "document_chunks",
        "evidence_spans",
        "entity_mentions",
        "graph_review_items",
        "memory_embedding_records",
    }

    assert expected.issubset(Base.metadata.tables)


def test_graph_memory_models_can_persist_relationships() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        document = Document(
            file_name="shaft.pdf",
            file_hash="graph-model-hash",
            file_size=10,
            mime_type="application/pdf",
            storage_path="documents/shaft.pdf",
            status=DocumentStatus.approved,
        )
        source = KnowledgeNode(
            node_type="document",
            title="Чертёж вала",
            entity_type="document",
            entity_id=document.id,
        )
        target = KnowledgeNode(
            node_type="material",
            title="Сталь 40Х",
            canonical_key="material:steel:40x",
        )
        session.add_all([document, source, target])
        session.flush()

        chunk = DocumentChunk(
            document_id=document.id,
            chunk_index=0,
            text="Материал Сталь 40Х. Токарная операция.",
        )
        session.add(chunk)
        session.flush()

        evidence = EvidenceSpan(
            document_id=document.id,
            chunk_id=chunk.id,
            field_name="material",
            text="Материал Сталь 40Х",
            confidence=0.95,
        )
        session.add(evidence)
        session.flush()

        edge = KnowledgeEdge(
            source_node_id=source.id,
            target_node_id=target.id,
            edge_type="mentions",
            confidence=0.9,
            reason="Материал указан в тексте документа.",
            source_document_id=document.id,
            evidence_span_id=evidence.id,
        )
        session.add(edge)
        session.commit()

        saved = session.get(KnowledgeEdge, edge.id)
        assert saved is not None
        assert saved.source.title == "Чертёж вала"
        assert saved.target.title == "Сталь 40Х"


def test_graph_memory_schemas_validate_ids_and_scores() -> None:
    entity_id = uuid.uuid4()
    node = KnowledgeNodeCreate(
        node_type="machine",
        title="Токарный станок 16К20",
        entity_type="machine",
        entity_id=entity_id,
        confidence=0.8,
    )
    hit = MemorySearchHit(
        kind="node",
        id=entity_id,
        title=node.title,
        score=0.75,
    )

    assert node.entity_id == entity_id
    assert hit.score == 0.75


def test_memory_explain_schema_carries_evidence_and_graph_context() -> None:
    entity_id = uuid.uuid4()
    response = MemoryExplainResponse(
        query="Сталь 40Х",
        hits=[
            MemorySearchHit(
                kind="node",
                id=entity_id,
                title="Сталь 40Х",
                score=1.0,
            )
        ],
        nodes=[],
        edges=[],
        evidence=[],
        total_hits=1,
    )

    assert response.query == "Сталь 40Х"
    assert response.total_hits == 1


def test_memory_search_schema_supports_sql_first_modes() -> None:
    request = MemorySearchRequest(
        query="контроль",
        retrieval_mode="sql_vector_rerank",
        include_explain=True,
    )
    hit = MemorySearchHit(
        kind="chunk",
        id=uuid.uuid4(),
        title="Document chunk #0",
        score=0.8,
        source="sql",
        text_score=0.8,
    )

    assert request.retrieval_mode == "sql_vector_rerank"
    assert hit.source == "sql"
    assert hit.text_score == 0.8


def test_memory_search_merges_sql_and_vector_candidates() -> None:
    hit_id = uuid.uuid4()
    merged = _merge_memory_hits(
        [
            MemorySearchHit(
                kind="chunk",
                id=hit_id,
                title="Document chunk #0",
                score=0.4,
                source="sql",
                text_score=0.4,
            ),
            MemorySearchHit(
                kind="chunk",
                id=hit_id,
                title="Document chunk #0",
                score=0.9,
                source="vector",
                vector_score=0.9,
            ),
        ]
    )

    assert len(merged) == 1
    assert merged[0].score == 0.9
    assert merged[0].source == "sql+vector"
    assert merged[0].text_score == 0.4
    assert merged[0].vector_score == 0.9


def test_memory_search_ranks_text_vector_graph_scores_with_weights() -> None:
    ranked = _rank_memory_hit(
        MemorySearchHit(
            kind="chunk",
            id=uuid.uuid4(),
            title="Document chunk #0",
            score=0.0,
            source="sql+vector+graph",
            text_score=0.5,
            vector_score=1.0,
            graph_score=0.2,
        )
    )

    assert ranked.score == 0.655


def test_memory_embedding_records_can_track_vector_indexing() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        document = Document(
            file_name="embedding.pdf",
            file_hash="graph-embedding-hash",
            file_size=10,
            mime_type="application/pdf",
            storage_path="documents/embedding.pdf",
            status=DocumentStatus.approved,
        )
        session.add(document)
        session.flush()
        chunk = DocumentChunk(
            document_id=document.id,
            chunk_index=0,
            text="Материал Сталь 40Х",
        )
        session.add(chunk)
        session.flush()
        record = MemoryEmbeddingRecord(
            content_type="document_chunk",
            content_id=chunk.id,
            document_id=document.id,
            collection_name="memory_chunks",
            point_id=f"chunk:{chunk.id}",
            embedding_model="nomic-embed-text",
            vector_size=768,
            status="queued",
        )
        chunk.embedding_id = record.point_id
        session.add(record)
        session.commit()

        saved = session.query(MemoryEmbeddingRecord).one()
        assert saved.point_id == chunk.embedding_id
        assert saved.status == "queued"


def test_memory_builder_creates_document_graph_from_text() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        document = Document(
            file_name="process.pdf",
            file_hash="graph-builder-hash",
            file_size=10,
            mime_type="application/pdf",
            storage_path="documents/process.pdf",
            status=DocumentStatus.approved,
        )
        session.add(document)
        session.flush()

        result = build_document_memory_sync(
            session,
            document,
            text=(
                "Материал Сталь 40Х. Операция выполняется на токарный станок 16К20. "
                "Контроль по ГОСТ 2789-73. Инструмент: резец проходной."
            ),
        )
        session.commit()

        assert result.chunks_created == 1
        assert result.mentions_created >= 3
        assert result.edges_created == result.mentions_created

        titles = {node.title for node in session.query(KnowledgeNode).all()}
        edge_types = {edge.edge_type for edge in session.query(KnowledgeEdge).all()}
        assert "process.pdf" in titles
        assert any("Сталь 40Х" in title for title in titles)
        assert any("ГОСТ 2789-73" in title for title in titles)
        assert {"specifies_material", "uses_machine", "uses_tool", "requires"}.issubset(edge_types)


def test_memory_builder_uses_compact_scope_for_plain_documents() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        document = Document(
            file_name="letter.txt",
            file_hash="graph-plain-hash",
            file_size=10,
            mime_type="text/plain",
            storage_path="documents/letter.txt",
            status=DocumentStatus.approved,
        )
        session.add(document)
        session.flush()

        result = build_document_memory_sync(
            session,
            document,
            text="Просим согласовать поставку. Материал Сталь 40Х указан справочно.",
        )
        session.commit()

        assert determine_graph_build_scope(document, "обычный текст") == "compact"
        assert result.chunks_created == 1
        assert result.mentions_created == 0
        assert session.query(KnowledgeNode).one().metadata_["build_scope"] == "compact"


def test_memory_builder_records_document_version_provenance() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        document = Document(
            file_name="versioned.pdf",
            file_hash="graph-versioned-hash",
            file_size=10,
            mime_type="application/pdf",
            storage_path="documents/versioned.pdf",
            status=DocumentStatus.approved,
        )
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=2,
            storage_path="documents/versioned-v2.pdf",
        )
        session.add(version)
        session.flush()

        build_document_memory_sync(
            session,
            document,
            text="Материал Сталь 40Х. Контроль по ГОСТ 2789-73.",
            rebuild=True,
        )
        session.commit()

        chunk = session.query(DocumentChunk).one()
        evidence = session.query(EvidenceSpan).one()
        mention = session.query(EntityMention).first()
        edge = session.query(KnowledgeEdge).first()
        node = (
            session.query(KnowledgeNode)
            .filter(KnowledgeNode.entity_type == "document", KnowledgeNode.entity_id == document.id)
            .one()
        )

        assert chunk.document_version_id == version.id
        assert evidence.document_version_id == version.id
        assert mention.document_version_id == version.id
        assert edge.source_document_version_id == version.id
        assert node.source_document_version_id == version.id


def test_memory_builder_creates_review_items_for_medium_confidence_edges() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        document = Document(
            file_name="route.pdf",
            file_hash="graph-review-hash",
            file_size=10,
            mime_type="application/pdf",
            storage_path="documents/route.pdf",
            status=DocumentStatus.approved,
        )
        session.add(document)
        session.flush()

        result = build_document_memory_sync(
            session,
            document,
            text="Материал Сталь 40Х. Контроль по ГОСТ 2789-73.",
        )
        session.commit()

        assert result.review_items_created == result.edges_created
        review_items = session.query(GraphReviewItem).all()
        assert len(review_items) == result.edges_created
        assert {item.status for item in review_items} == {"pending"}


def test_memory_builder_rebuild_is_idempotent_for_auto_layer() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        document = Document(
            file_name="rebuild.pdf",
            file_hash="graph-rebuild-hash",
            file_size=10,
            mime_type="application/pdf",
            storage_path="documents/rebuild.pdf",
            status=DocumentStatus.approved,
        )
        session.add(document)
        session.flush()

        text = "Материал Сталь 40Х. Инструмент: резец проходной. Контроль по ГОСТ 2789-73."
        first = build_document_memory_sync(session, document, text=text, rebuild=True)
        session.commit()
        second = build_document_memory_sync(session, document, text=text, rebuild=True)
        session.commit()

        assert second.mentions_created == first.mentions_created
        assert second.edges_created == first.edges_created
        assert session.query(DocumentChunk).count() == first.chunks_created
        assert session.query(EntityMention).count() == first.mentions_created
        assert session.query(KnowledgeEdge).count() == first.edges_created
        assert session.query(GraphReviewItem).count() == first.review_items_created
        assert (
            session.query(KnowledgeNode)
            .filter(KnowledgeNode.entity_type == "document", KnowledgeNode.entity_id == document.id)
            .count()
            == 1
        )


def test_memory_builder_flags_potential_document_conflicts() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        document = Document(
            file_name="conflict.pdf",
            file_hash="graph-conflict-hash",
            file_size=10,
            mime_type="application/pdf",
            storage_path="documents/conflict.pdf",
            status=DocumentStatus.approved,
        )
        session.add(document)
        session.flush()

        result = build_document_memory_sync(
            session,
            document,
            text="Материал Сталь 40Х. Альтернативно указан материал Сталь 45.",
            rebuild=True,
            build_scope="extended",
        )
        session.commit()

        conflict_edges = (
            session.query(KnowledgeEdge).filter(KnowledgeEdge.edge_type == "conflicts_with").all()
        )
        assert len(conflict_edges) == 1
        assert conflict_edges[0].confidence == 0.6
        assert result.review_items_created == result.edges_created
        pending_reviews = (
            session.query(GraphReviewItem).filter(GraphReviewItem.status == "pending").count()
        )
        assert pending_reviews == result.edges_created
