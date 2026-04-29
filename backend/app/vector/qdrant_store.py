"""Qdrant vector store — collection management, upsert, search."""

import structlog
import uuid
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    Query,
    VectorParams,
)

from app.config import settings

logger = structlog.get_logger()

COLLECTION = "documents"
VECTOR_SIZE = 768


def get_client() -> QdrantClient:
    return QdrantClient(url=settings.qdrant_url, timeout=10)


def ensure_collection(
    collection_name: str = COLLECTION,
    vector_size: int = VECTOR_SIZE,
    distance_metric: str = "cosine",
) -> None:
    """Create Qdrant collection if it doesn't exist."""
    client = get_client()
    existing = {c.name for c in client.get_collections().collections}
    if collection_name not in existing:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_size, distance=_distance(distance_metric)),
        )
        logger.info("qdrant_collection_created", collection=collection_name)
    else:
        logger.debug("qdrant_collection_exists", collection=collection_name)


def upsert_document(
    doc_id: str,
    vector: list[float],
    *,
    file_name: str,
    doc_type: str | None,
    status: str,
    source_channel: str | None = None,
    collection_name: str = COLLECTION,
    embedding_model: str | None = None,
) -> None:
    """Upsert document embedding into Qdrant."""
    client = get_client()
    client.upsert(
        collection_name=collection_name,
        points=[
            PointStruct(
                id=_uuid_to_uint64(doc_id),
                vector=vector,
                payload={
                    "doc_id": doc_id,
                    "file_name": file_name,
                    "doc_type": doc_type or "",
                    "status": status,
                    "source_channel": source_channel or "",
                    "embedding_model": embedding_model or "",
                },
            )
        ],
    )


def upsert_memory_embedding(
    *,
    point_id: str,
    vector: list[float],
    collection_name: str,
    payload: dict,
) -> None:
    client = get_client()
    client.upsert(
        collection_name=collection_name,
        points=[
            PointStruct(
                id=_stable_point_uuid(point_id),
                vector=vector,
                payload={**payload, "point_id": point_id},
            )
        ],
    )


def search_similar(
    query_vector: list[float],
    *,
    limit: int = 20,
    doc_type: str | None = None,
    status: str | None = None,
    score_threshold: float = 0.0,
    collection_name: str = COLLECTION,
) -> list[dict]:
    """Search Qdrant for similar documents. Returns list of {doc_id, score, payload}."""
    client = get_client()

    must = []
    if doc_type:
        must.append(FieldCondition(key="doc_type", match=MatchValue(value=doc_type)))
    if status:
        must.append(FieldCondition(key="status", match=MatchValue(value=status)))

    query_filter = Filter(must=must) if must else None

    response = client.query_points(
        collection_name=collection_name,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        score_threshold=score_threshold,
    )

    return [
        {
            "doc_id": hit.payload.get("doc_id", ""),
            "score": hit.score,
            "file_name": hit.payload.get("file_name", ""),
            "doc_type": hit.payload.get("doc_type", ""),
            "status": hit.payload.get("status", ""),
            "payload": hit.payload or {},
        }
        for hit in response.points
    ]


def delete_document(doc_id: str) -> None:
    from qdrant_client.models import PointIdsList
    client = get_client()
    client.delete(
        collection_name=COLLECTION,
        points_selector=PointIdsList(points=[_uuid_to_uint64(doc_id)]),
    )


def collection_count() -> int:
    return collection_count_for(COLLECTION)


def collection_count_for(collection_name: str = COLLECTION) -> int:
    client = get_client()
    try:
        info = client.get_collection(collection_name)
        return info.points_count or 0
    except Exception:
        return 0


def _uuid_to_uint64(uuid_str: str) -> int:
    """Convert UUID string to uint64 for Qdrant point ID."""
    import uuid as uuid_mod
    return uuid_mod.UUID(uuid_str).int & 0xFFFFFFFFFFFFFFFF


def _stable_point_uuid(point_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"document-invoices-ai:{point_id}"))


def _distance(distance_metric: str) -> Distance:
    normalized = distance_metric.lower()
    if normalized == "dot":
        return Distance.DOT
    if normalized == "euclid":
        return Distance.EUCLID
    return Distance.COSINE
