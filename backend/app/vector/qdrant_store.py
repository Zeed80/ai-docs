"""Qdrant vector store — collection management, upsert, search."""

import structlog
import uuid
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    HnswConfigDiff,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    Query,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    VectorParams,
)

from app.config import settings

logger = structlog.get_logger()

COLLECTION = "documents"
COLLECTION_DRAWINGS = "drawings"
COLLECTION_DRAWING_FEATURES = "drawing_features"
COLLECTION_TOOL_CATALOG = "tool_catalog"
VECTOR_SIZE = 4096


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
            vectors_config=VectorParams(
                size=vector_size,
                distance=_distance(distance_metric),
                hnsw_config=HnswConfigDiff(m=16, ef_construct=100),
            ),
            quantization_config=ScalarQuantization(
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8,
                    always_ram=True,
                )
            ),
        )
        for field, schema_type in [
            ("doc_type", PayloadSchemaType.KEYWORD),
            ("status", PayloadSchemaType.KEYWORD),
        ]:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=schema_type,
            )
        logger.info("qdrant_collection_created", collection=collection_name)
    else:
        logger.debug("qdrant_collection_exists", collection=collection_name)


def ensure_drawing_collections(vector_size: int = VECTOR_SIZE) -> None:
    """Create drawing-related Qdrant collections if they don't exist."""
    for collection_name, payload_indexes in [
        (
            COLLECTION_DRAWINGS,
            [("status", PayloadSchemaType.KEYWORD), ("drawing_number", PayloadSchemaType.KEYWORD)],
        ),
        (
            COLLECTION_DRAWING_FEATURES,
            [("drawing_id", PayloadSchemaType.KEYWORD), ("feature_type", PayloadSchemaType.KEYWORD)],
        ),
        (
            COLLECTION_TOOL_CATALOG,
            [("tool_type", PayloadSchemaType.KEYWORD), ("supplier_id", PayloadSchemaType.KEYWORD),
             ("is_active", PayloadSchemaType.KEYWORD)],
        ),
    ]:
        ensure_collection(collection_name=collection_name, vector_size=vector_size)
        client = get_client()
        existing_collection = client.get_collection(collection_name)
        for field, schema_type in payload_indexes:
            try:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=schema_type,
                )
            except Exception:
                pass


def upsert_drawing(
    drawing_id: str,
    vector: list[float],
    *,
    drawing_number: str | None,
    status: str,
    filename: str,
    title: str | None = None,
    embedding_model: str | None = None,
) -> None:
    """Upsert drawing embedding into Qdrant."""
    client = get_client()
    client.upsert(
        collection_name=COLLECTION_DRAWINGS,
        points=[
            PointStruct(
                id=_stable_point_uuid(f"drawing:{drawing_id}"),
                vector=vector,
                payload={
                    "drawing_id": drawing_id,
                    "drawing_number": drawing_number or "",
                    "status": status,
                    "filename": filename,
                    "title": title or "",
                    "embedding_model": embedding_model or "",
                },
            )
        ],
    )


def upsert_drawing_feature(
    feature_id: str,
    vector: list[float],
    *,
    drawing_id: str,
    feature_type: str,
    name: str,
    description: str | None = None,
    embedding_model: str | None = None,
) -> None:
    """Upsert drawing feature embedding into Qdrant."""
    client = get_client()
    client.upsert(
        collection_name=COLLECTION_DRAWING_FEATURES,
        points=[
            PointStruct(
                id=_stable_point_uuid(f"drawing_feature:{feature_id}"),
                vector=vector,
                payload={
                    "feature_id": feature_id,
                    "drawing_id": drawing_id,
                    "feature_type": feature_type,
                    "name": name,
                    "description": description or "",
                    "embedding_model": embedding_model or "",
                },
            )
        ],
    )


def upsert_tool_catalog_entry(
    entry_id: str,
    vector: list[float],
    *,
    tool_type: str,
    name: str,
    supplier_id: str | None = None,
    diameter_mm: float | None = None,
    material: str | None = None,
    is_active: bool = True,
    embedding_model: str | None = None,
) -> None:
    """Upsert tool catalog entry embedding into Qdrant."""
    client = get_client()
    client.upsert(
        collection_name=COLLECTION_TOOL_CATALOG,
        points=[
            PointStruct(
                id=_stable_point_uuid(f"tool_catalog:{entry_id}"),
                vector=vector,
                payload={
                    "entry_id": entry_id,
                    "tool_type": tool_type,
                    "name": name,
                    "supplier_id": supplier_id or "",
                    "diameter_mm": diameter_mm,
                    "material": material or "",
                    "is_active": str(is_active).lower(),
                    "embedding_model": embedding_model or "",
                },
            )
        ],
    )


def search_drawing_features(
    query_vector: list[float],
    *,
    drawing_id: str | None = None,
    feature_type: str | None = None,
    limit: int = 20,
    score_threshold: float = 0.0,
) -> list[dict]:
    """Search drawing features by embedding similarity."""
    client = get_client()
    must = []
    if drawing_id:
        must.append(FieldCondition(key="drawing_id", match=MatchValue(value=drawing_id)))
    if feature_type:
        must.append(FieldCondition(key="feature_type", match=MatchValue(value=feature_type)))
    query_filter = Filter(must=must) if must else None
    response = client.query_points(
        collection_name=COLLECTION_DRAWING_FEATURES,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        score_threshold=score_threshold,
    )
    return [
        {
            "feature_id": hit.payload.get("feature_id", ""),
            "drawing_id": hit.payload.get("drawing_id", ""),
            "feature_type": hit.payload.get("feature_type", ""),
            "name": hit.payload.get("name", ""),
            "description": hit.payload.get("description", ""),
            "score": hit.score,
            "payload": hit.payload or {},
        }
        for hit in response.points
    ]


def search_tool_catalog(
    query_vector: list[float],
    *,
    tool_type: str | None = None,
    supplier_id: str | None = None,
    limit: int = 20,
    score_threshold: float = 0.0,
) -> list[dict]:
    """Search tool catalog entries by embedding similarity."""
    client = get_client()
    must: list = [FieldCondition(key="is_active", match=MatchValue(value="true"))]
    if tool_type:
        must.append(FieldCondition(key="tool_type", match=MatchValue(value=tool_type)))
    if supplier_id:
        must.append(FieldCondition(key="supplier_id", match=MatchValue(value=supplier_id)))
    query_filter = Filter(must=must)
    response = client.query_points(
        collection_name=COLLECTION_TOOL_CATALOG,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        score_threshold=score_threshold,
    )
    return [
        {
            "entry_id": hit.payload.get("entry_id", ""),
            "tool_type": hit.payload.get("tool_type", ""),
            "name": hit.payload.get("name", ""),
            "supplier_id": hit.payload.get("supplier_id", ""),
            "diameter_mm": hit.payload.get("diameter_mm"),
            "material": hit.payload.get("material", ""),
            "score": hit.score,
            "payload": hit.payload or {},
        }
        for hit in response.points
    ]


def delete_drawing(drawing_id: str) -> None:
    """Delete all Qdrant points for a drawing."""
    from qdrant_client.models import FilterSelector
    client = get_client()
    for collection in [COLLECTION_DRAWINGS, COLLECTION_DRAWING_FEATURES]:
        id_field = "drawing_id" if collection == COLLECTION_DRAWING_FEATURES else None
        try:
            if collection == COLLECTION_DRAWINGS:
                client.delete(
                    collection_name=collection,
                    points_selector=FilterSelector(
                        filter=Filter(
                            must=[FieldCondition(key="drawing_id", match=MatchValue(value=drawing_id))]
                        )
                    ),
                )
            else:
                client.delete(
                    collection_name=collection,
                    points_selector=FilterSelector(
                        filter=Filter(
                            must=[FieldCondition(key="drawing_id", match=MatchValue(value=drawing_id))]
                        )
                    ),
                )
        except Exception:
            pass


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


def delete_tool_catalog_entry(entry_id: str) -> None:
    """Delete a single tool catalog entry from Qdrant by entry_id."""
    from qdrant_client.models import FilterSelector
    client = get_client()
    try:
        client.delete(
            collection_name=COLLECTION_TOOL_CATALOG,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="entry_id", match=MatchValue(value=entry_id))]
                )
            ),
        )
    except Exception as exc:
        logger.warning("qdrant_delete_tool_entry_failed", entry_id=entry_id, error=str(exc))


def delete_tool_catalog_by_supplier(supplier_id: str) -> None:
    """Delete all tool catalog entries for a given supplier from Qdrant."""
    from qdrant_client.models import FilterSelector
    client = get_client()
    try:
        client.delete(
            collection_name=COLLECTION_TOOL_CATALOG,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="supplier_id", match=MatchValue(value=supplier_id))]
                )
            ),
        )
    except Exception as exc:
        logger.warning("qdrant_delete_supplier_catalog_failed", supplier_id=supplier_id, error=str(exc))


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
